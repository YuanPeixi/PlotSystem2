"""场景引擎：驱动多角色对话，管理场景生命周期。

实现要点（CLAUDE.md 5.4 / 7.4）：
- run() 开头必须创建模拟前快照
- 每轮解析对白/动作/内心独白
- 终止条件检查
- 结束创建模拟后快照
- 支持 turn 回调用于 SSE 流式推送
"""

from __future__ import annotations

import random
import re
from collections.abc import Awaitable, Callable

from backend.agents.character_agent import CharacterAgent
from backend.models import (
    CharacterState,
    DialogueTurn,
    Scene,
    SceneConfig,
    SceneResult,
    SceneStatus,
    SpeakerMode,
    new_id,
)
from backend.scene_engine.speaker_selector import ScoringSpeakerSelector, SelectionTrace
from backend.scene_engine.termination import check_termination
from backend.snapshot import SnapshotManager
from backend.utils.logger import get_logger

logger = get_logger("scene_engine")

TurnCallback = Callable[[DialogueTurn], Awaitable[None]] | None

# 解析格式：*动作*、[内心独白]、其余为对白
_ACTION_RE = re.compile(r"\*(.+?)\*", re.DOTALL)
_THOUGHT_RE = re.compile(r"\[(.+?)\]", re.DOTALL)

_KNOWN_SPEAKER_MODES = {m.value for m in SpeakerMode}


def _selector_notice(trace: SelectionTrace) -> str:
    """把 selector 的降级情况压成一句给前端展示的短提示，正常时为空串。"""
    if trace.degraded:
        return "服务不可用：降级选择"
    if trace.llm_failures:
        return f"{trace.llm_failures}/{len(trace.scores)} 打分失败：已兜底"
    return ""


class SceneEngine:
    """场景执行引擎。"""

    def __init__(
        self,
        scene: Scene,
        scene_config: SceneConfig,
        character_agents: list[CharacterAgent],
        snapshot_manager: SnapshotManager,
    ):
        self.scene = scene
        self.config = scene_config
        self.agents = character_agents
        self.snapshot_manager = snapshot_manager
        self._interrupt = False
        self._history_transcript: list[str] = []  # continue 时注入的历史
        self._selector: ScoringSpeakerSelector | None = None
        self._unknown_mode_warned = False

    def interrupt(self) -> None:
        """外部请求中断（如导演/暂停）。"""
        self._interrupt = True

    def inject_history(self, history_log: list[DialogueTurn]) -> None:
        """将历史对话轮次注入引擎，供 continue 续跑时使用。"""
        self._history_transcript = [self._turn_line(t) for t in history_log]

    async def run(self, on_turn: TurnCallback = None) -> SceneResult:
        """场景执行主流程。"""
        if not self.agents:
            raise ValueError("场景至少需要一个角色")

        # 1. 模拟前快照（continue 时保留原有 snapshot_id_before，不重复打快照）
        if not self.scene.snapshot_id_before:
            before_states = self._collect_states()
            snap_before = await self.snapshot_manager.create_snapshot(
                scene_id=self.scene.scene_id,
                branch_id=self.scene.branch_id,
                character_states=before_states,
                scene_context=self._scene_context(),
                label=f"before:{self.config.name}",
            )
            self.scene.snapshot_id_before = snap_before.snapshot_id
        self.scene.status = SceneStatus.RUNNING.value

        # 2. 连接各角色记忆
        for agent in self.agents:
            await agent.memory.connect()

        # 3. 对话循环
        # continue 续跑时先把历史 transcript 放入上下文
        turns: list[DialogueTurn] = list(self.scene.dialogue_log)  # 保留已有轮次
        transcript: list[str] = list(self._history_transcript)
        if not transcript and self.config.opening_narration:
            transcript.append(f"【旁白】{self.config.opening_narration}")

        # turn_number 从历史轮次末尾续接（continue 时不从 0 开始）
        turn_number = len(turns)
        new_turns: list[DialogueTurn] = []  # 本次新增轮次（用于记忆固化）
        terminated_reason = ""
        while True:
            stop, reason = check_termination(
                turns, self.config.max_turns, self._interrupt
            )
            if stop:
                terminated_reason = reason
                break

            agent, selector_notice = await self._select_speaker(turn_number, transcript, turns)
            raw = await agent.respond(self._scene_context(), transcript)
            turn_number += 1
            turn = self._parse_turn(raw, agent, turn_number)
            turn.selector_notice = selector_notice
            turns.append(turn)
            new_turns.append(turn)
            transcript.append(self._turn_line(turn))

            # 在场即记忆（工单15）：本场全部参演角色都感知到这一轮，而不只是发言者；
            # 对非发言者剥离内心独白，避免私有内心泄露给旁观角色（契约1）。
            for participant in self.agents:
                await participant.memory.add_experience(
                    turn, from_self=(participant.character_id == turn.character_id)
                )

            if on_turn:
                await on_turn(turn)

        # 4. 固化记忆（必须先于后置快照！只固化本次新增轮次，历史轮次在上次
        # 结束时已固化）。consolidate 会清空 short_term 缓冲。若顺序颠倒——
        # 先打快照再固化——快照里的 short_term_buffer 会带着"即将被固化"的
        # 原始文本；一旦这份快照后续被 continue/rollback/next_scene 用于
        # prime() 回填新场景的记忆，这批本已写入长期记忆的台词会随新场景的
        # 下一次 consolidate 被二次写入长期记忆（跨场景重复，且长期记忆按
        # 角色+项目共享、不随分支回滚，重复只会累积不会自愈）。
        for agent in self.agents:
            await agent.update_state_after_scene(new_turns)

        # 5. 模拟后快照（此时短期缓冲已清空，快照记录的是"已落库"的干净状态，
        # 供下一场 prime() 回填也不会重新引入已固化过的内容）
        after_states = self._collect_states()
        snap_after = await self.snapshot_manager.create_snapshot(
            scene_id=self.scene.scene_id,
            branch_id=self.scene.branch_id,
            character_states=after_states,
            scene_context=self._scene_context(),
            label=f"after:{self.config.name}",
        )

        self.scene.dialogue_log = turns
        self.scene.turns_completed = turn_number
        self.scene.snapshot_id_after = snap_after.snapshot_id
        self.scene.status = SceneStatus.COMPLETED.value

        return SceneResult(
            scene_id=self.scene.scene_id,
            dialogue_log=turns,
            snapshot_id_before=self.scene.snapshot_id_before,
            snapshot_id_after=snap_after.snapshot_id,
            turns_completed=turn_number,
            terminated_reason=terminated_reason,
        )

    # ---- 发言者选择 ----
    async def _select_speaker(
        self, turn_number: int, transcript: list[str], turns: list[DialogueTurn]
    ) -> tuple[CharacterAgent, str]:
        """选出下一个发言者，并返回一句可展示给用户的降级提示（正常为空串）。"""
        mode = self.config.speaker_mode
        if mode == SpeakerMode.RANDOM.value:
            return random.choice(self.agents), ""
        # 首轮没有任何已发生的轮次，无从评分，直接按出场顺序开场
        if mode == SpeakerMode.SELECTOR.value and turns:
            if self._selector is None:
                self._selector = ScoringSpeakerSelector(self.agents)
            agent, trace = await self._selector.select(transcript, turns)
            return agent, _selector_notice(trace)
        if mode not in _KNOWN_SPEAKER_MODES and not self._unknown_mode_warned:
            # 只警告一次，避免每轮刷屏；数据来源可能是绕过 API 写入的历史脏数据
            self._unknown_mode_warned = True
            logger.warning(
                "[scene] 场景 %s 的 speaker_mode=%r 不是合法取值，本场回退 round_robin",
                self.scene.scene_id,
                mode,
            )
        # 默认 round_robin
        return self.agents[turn_number % len(self.agents)], ""

    # ---- 解析 ----
    def _parse_turn(self, raw: str, agent: CharacterAgent, turn_number: int) -> DialogueTurn:
        actions = _ACTION_RE.findall(raw)
        thoughts = _THOUGHT_RE.findall(raw)
        # 去掉动作与独白后剩余即为对白
        dialogue = _THOUGHT_RE.sub("", _ACTION_RE.sub("", raw)).strip()
        dialogue = re.sub(r"\s+", " ", dialogue).strip()

        return DialogueTurn(
            turn_id=new_id(),
            scene_id=self.scene.scene_id,
            turn_number=turn_number,
            character_id=agent.character_id,
            character_name=agent.name,
            dialogue=dialogue or None,
            action="；".join(a.strip() for a in actions) or None,
            inner_thought="；".join(t.strip() for t in thoughts) or None,
        )

    @staticmethod
    def _turn_line(turn: DialogueTurn) -> str:
        parts = []
        if turn.action:
            parts.append(f"*{turn.action}*")
        if turn.dialogue:
            parts.append(turn.dialogue)
        return f"{turn.character_name}: {' '.join(parts)}".strip()

    # ---- 上下文/状态 ----
    def _scene_context(self) -> dict:
        return {
            "name": self.config.name,
            "description": self.config.description,
            "location": self.config.location,
            "opening_narration": self.config.opening_narration,
            **self.config.initial_conditions,
        }

    def _collect_states(self) -> dict[str, CharacterState]:
        states: dict[str, CharacterState] = {}
        for agent in self.agents:
            card = agent.card
            states[agent.character_id] = CharacterState(
                character_id=agent.character_id,
                current_emotion=card.current_emotion,
                current_goal=card.current_goal,
                current_location=card.current_location or self.config.location,
                relationships=dict(card.relationships),
                episodic_summary=agent.memory.episodic.dump(),
                short_term_buffer=agent.memory.short_term.dump(),
            )
        return states
