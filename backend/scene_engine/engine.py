"""场景引擎：驱动多角色对话，管理场景生命周期。

实现要点（CLAUDE.md 5.4 / 7.4）：
- run() 开头必须创建模拟前快照
- 每轮解析对白/动作/内心独白
- 终止条件检查
- 结束创建模拟后快照
- 支持 turn 回调用于 SSE 流式推送
"""

from __future__ import annotations

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
from backend.scene_engine.termination import check_termination
from backend.snapshot import SnapshotManager
from backend.utils.llm import chat_safe
from backend.utils.logger import get_logger

logger = get_logger("scene_engine")

TurnCallback = Callable[[DialogueTurn], Awaitable[None]] | None

# 解析格式：*动作*、[内心独白]、其余为对白
_ACTION_RE = re.compile(r"\*(.+?)\*", re.DOTALL)
_THOUGHT_RE = re.compile(r"\[(.+?)\]", re.DOTALL)


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

            agent = await self._select_speaker(turn_number, transcript)
            raw = await agent.respond(self._scene_context(), transcript)
            turn_number += 1
            turn = self._parse_turn(raw, agent, turn_number)
            turns.append(turn)
            new_turns.append(turn)
            transcript.append(self._turn_line(turn))

            # 实时写入该角色记忆
            await agent.memory.add_experience(turn)

            if on_turn:
                await on_turn(turn)

        # 4. 模拟后快照
        after_states = self._collect_states()
        snap_after = await self.snapshot_manager.create_snapshot(
            scene_id=self.scene.scene_id,
            branch_id=self.scene.branch_id,
            character_states=after_states,
            scene_context=self._scene_context(),
            label=f"after:{self.config.name}",
        )

        # 5. 固化记忆（只固化本次新增轮次，历史轮次在上次结束时已固化）
        for agent in self.agents:
            await agent.update_state_after_scene(new_turns)

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
    async def _select_speaker(self, turn_number: int, transcript: list[str]) -> CharacterAgent:
        mode = self.config.speaker_mode
        if mode == SpeakerMode.RANDOM.value:
            import random

            return random.choice(self.agents)
        if mode == SpeakerMode.SELECTOR.value and transcript:
            return await self._llm_select_speaker(transcript)
        # 默认 round_robin
        return self.agents[turn_number % len(self.agents)]

    async def _llm_select_speaker(self, transcript: list[str]) -> CharacterAgent:
        names = [a.name for a in self.agents]
        recent = "\n".join(transcript[-6:])
        prompt = (
            f"以下是场景对话片段：\n{recent}\n\n"
            f"候选发言者：{', '.join(names)}\n"
            f"谁最适合接下来发言？只回答一个名字。"
        )
        try:
            choice = (await chat_safe([{"role": "user", "content": prompt}], temperature=0.3)).strip()
            for agent in self.agents:
                if agent.name in choice:
                    return agent
        except Exception:  # noqa: BLE001
            pass
        return self.agents[0]

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
