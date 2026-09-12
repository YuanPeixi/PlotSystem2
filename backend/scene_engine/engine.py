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
from backend.config import settings
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
# 把当前 scene 落库（不推 SSE），用于固化水位线这类不伴随新轮次的进展。
PersistCallback = Callable[[], Awaitable[None]] | None

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

    async def run(
        self, on_turn: TurnCallback = None, on_persist: PersistCallback = None
    ) -> SceneResult:
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
                story_history=self.scene.inherited_story_history,
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

        # 崩溃/异常中断后的续跑：这段轮次已逐轮落盘，但固化只在场景正常结束时
        # 发生，它们还没进过任何一层记忆。不补回去就是"对话还在，但角色忘了"。
        # 重放同样要走周期固化：短期缓冲是定长 deque，一次补回上百轮会静默淘汰
        # 最早的内容（旧实现由 add_experience 的自动固化兜住，工单26 已移走）。
        #
        # 两个起点不能合并（工单26 复盘）：
        # - 长期记忆从 turns_consolidated 起补，早于水位线的已经入库，重放会重复；
        # - 事件摘要（episodic）是**独立的内存层**，快照之外无处续命，水位线跟它
        #   毫无关系。按水位线切会让 [0, turns_consolidated) 这段的重要事件在
        #   续跑后彻底消失。这个洞在工单26 之前就有，只是那时水位线中途不推进、
        #   崩溃时通常为 0，重放恰好覆盖全部而掩盖了它。
        await self._replay_unconsolidated(turns, on_persist)

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
            transcript.append(self._turn_line(turn))
            await self._remember(turn)

            # 先把进度写回 scene 再回调：编排层的 on_turn 会据此逐轮落盘，
            # 中途刷新/断线/进程退出时已产生的轮次才不会丢（工单23）。
            self.scene.dialogue_log = list(turns)
            self.scene.turns_completed = turn_number

            # 周期固化必须早于 on_turn：后者会推 SSE，而落盘必须先于推送（工单23），
            # 所以走不推事件的 on_persist（工单26 红线 R1）。
            if self._should_consolidate(len(turns)):
                # 固化前先把对话日志单独落一次盘（工单26 复盘）。Chroma 与 SQLite
                # 是两个库、没有跨库事务，写完长期记忆到水位线落盘之间必然存在
                # 一个窗口；能选的只是**往哪边失衡**：
                # - 不落这一次：窗口内崩溃 → 角色记得一句日志里还没有的台词；
                # - 先落日志：窗口内崩溃 → 日志有、水位线旧 → 续跑重放该轮 →
                #   撞上内容寻址的幂等兜底（long_term.memory_id），收敛成一条。
                # 后者把一个没有兜底的失败模式换成了一个已有兜底的，故取后者。
                # 不能改成"固化挪到 on_turn 之后"：那会让 SSE 推送插进固化与落盘
                # 之间，破坏 R1。
                if on_persist:
                    await on_persist()
                await self._consolidate_all(len(turns), on_persist)

            if on_turn:
                await on_turn(turn)

        # 4. 收尾固化（必须先于后置快照！）。consolidate 会清空 short_term 缓冲，
        # 把周期固化之后剩下的尾巴写进长期记忆。若顺序颠倒——
        # 先打快照再固化——快照里的 short_term_buffer 会带着"即将被固化"的
        # 原始文本；一旦这份快照后续被 continue/rollback/next_scene 用于
        # prime() 回填新场景的记忆，这批本已写入长期记忆的台词会随新场景的
        # 下一次 consolidate 被二次写入长期记忆（跨场景重复，且长期记忆按
        # 角色+项目共享、不随分支回滚，重复只会累积不会自愈）。
        #
        # 水位线必须与固化写入在同一次落盘内推进：下一步的后置快照要拷贝整个
        # kuzu/chroma（十几到二十多兆），进程若在这段窗口里被硬杀（非异常，走不到
        # orchestrator 的 except），库里仍是旧水位线，续跑会把整场对话二次写入长期记忆。
        await self._consolidate_all(len(turns), on_persist)

        # 5. 模拟后快照（此时短期缓冲已清空，快照记录的是"已落库"的干净状态，
        # 供下一场 prime() 回填也不会重新引入已固化过的内容）
        after_states = self._collect_states()
        snap_after = await self.snapshot_manager.create_snapshot(
            scene_id=self.scene.scene_id,
            branch_id=self.scene.branch_id,
            character_states=after_states,
            scene_context=self._scene_context(),
            label=f"after:{self.config.name}",
            story_history=self.scene.inherited_story_history,
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

    # ---- 记忆固化 ----
    # 缓冲占用率超过此值就强制固化，不等固化周期到点。定长 deque 写满后会静默
    # 淘汰最早的条目，而那些内容还没进过长期记忆 —— 配置校验挡不住全部情况
    # （周期=0 时跨度是整场 max_turns；prime() 回填会让场景开跑时缓冲就非空），
    # 所以运行时必须再兜一道。0.75 留出一个周期左右的余量。
    _BUFFER_PRESSURE_LIMIT = 0.75

    def _buffer_under_pressure(self) -> bool:
        """是否有角色的短期缓冲逼近容量上限，再不固化就要丢内容了。"""
        return any(
            agent.memory.short_term.pressure() >= self._BUFFER_PRESSURE_LIMIT
            for agent in self.agents
        )

    def _should_consolidate(self, turns_done: int) -> bool:
        """是否该固化了。两个独立闸门，任一满足即触发。

        1. 距上次固化已满一个周期（正常节奏）；
        2. 有角色的缓冲逼近容量（兜底）—— 周期 <=0 或周期配得过大时，
           只看第 1 条会让超出容量的轮次在固化前就被 deque 淘汰，
           而水位线照推，等于宣称已入库、实际永久丢失（工单26 复盘）。
        """
        if self._buffer_under_pressure():
            return True
        every = settings.MEMORY_CONSOLIDATE_EVERY_TURNS
        if every <= 0:
            return False
        return turns_done - self.scene.turns_consolidated >= every

    async def _consolidate_all(self, turns_done: int, on_persist: PersistCallback) -> None:
        """写长期记忆 → 推进水位线 → 立即落盘。三步是一个原子单元，不可拆开。

        中间任一步之后被硬杀，库里的 `turns_consolidated` 都必须与长期记忆里
        实际已有的内容对齐，否则续跑的重放会二次写入（工单26）。
        """
        for agent in self.agents:
            await agent.consolidate_memory()
        self.scene.turns_consolidated = turns_done
        if on_persist:
            await on_persist()

    # ---- 记忆写入 ----
    async def _replay_unconsolidated(
        self, turns: list[DialogueTurn], on_persist: PersistCallback
    ) -> None:
        """续跑时把已落盘但未进记忆的轮次补回去，两层各按自己的起点。"""
        watermark = self.scene.turns_consolidated
        # 水位线之前的轮次：长期记忆里已经有了，只补事件摘要。整批交给
        # replay_episodic 去重重建 —— prime() 可能已载入其中一部分（工单26 复盘·
        # 断言4），逐轮追加会让正常 continue 把这段翻倍并挤掉更早场次的事件。
        self._replay_episodic(turns[:watermark])
        # 水位线之后的轮次：三层都没有，走完整写入 + 周期固化。
        replayed = watermark
        for past in turns[watermark:]:
            await self._remember(past)
            replayed += 1
            if self._should_consolidate(replayed):
                await self._consolidate_all(replayed, on_persist)

    def _replay_episodic(self, turns: list[DialogueTurn]) -> None:
        """只重建事件摘要，不碰短期缓冲。

        这批轮次的正文已在长期记忆里，再写一遍缓冲就会被下一次固化二次写入
        （工单26 主线要修的正是这个）。但 episodic 是纯内存的独立一层，
        不补就永久缺失这一段的重要事件。
        """
        if not turns:
            return
        for participant in self.agents:
            participant.memory.replay_episodic(turns)

    async def _remember(self, turn: DialogueTurn) -> None:
        """在场即记忆（工单15）：本场全部参演角色都感知这一轮，不只是发言者。

        对非发言者剥离内心独白，避免私有内心泄露给旁观角色（契约1）。
        """
        for participant in self.agents:
            await participant.memory.add_experience(
                turn, from_self=(participant.character_id == turn.character_id)
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
