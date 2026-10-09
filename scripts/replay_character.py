"""角色回复的回放评测（工单30b）：同一个现场、几种 prompt 变体，比较续写污染率。

用法：
    python -m scripts.replay_character --project PROJECT_ID \\
        --site SCENE_ID:TURN [--site ...] [--variants base,A,B,C,A+B,t0.5] [-n 5] \\
        [--memory empty|checkpoint] [--mode off|record|adjudicate] [--concurrency 4] [--run] [--out PATH]

`--site SCENE:TURN` 取"某场第 TURN 轮那位角色、在那一轮之前的现场"，场景 ID 可写前缀。不带 `--run`
只重建现场并打印将发起的调用次数，**不调 LLM**；带 `--run` 才真正生成（每次运行都调 LLM、花钱）。

判定：
- **污染**：用 30a 的 `trim_continuation` 判（引擎落库前的同一道截断），截掉部分非空即污染；
- **变体特有形态**：B 改了"目前对话"的写法，模型照抄新写法续写时 `trim_continuation`
  认不出（它只认现行格式的标记）。这一列单独统计、不并进污染率，免得 B 靠"换了个检测不到的
  写法"显得更好。没有标记的续写（直接用叙述口吻写出结果）两列都抓不到，只能人读样本；
- **空回复**：截掉续写后对白、动作、独白全空；**调用失败**：`LLMError`（含服务商的空正文，
  `utils/llm.py` 已退避重试过）。单次采样不做引擎那次"截完为空再要一次"。

现场重建的口径（输出文件开头会再写一遍）：
- 场景上下文、【在场物件】、R7、【当前环境】都由真实的 `SceneEngine` 算出；世界变量与角色
  时点状态取该场的前置快照；人设取**当前**角色卡（事后编辑过会漂移）；
- "目前对话" = 开场白一行 + 该轮之前的全部轮次，逐行经引擎的 `_turn_line`。若该轮属于
  continue 续跑段，真实现场没有开场白那一行（段边界没落盘，无从得知）；超预算时的窗口起点
  按本角色此前每次发言从场景开头推演，continue 段的归零同样还原不了；
- 档位没落盘：有环境回合或裁决过的动作就按 adjudicate，可用 `--mode` 覆盖。【当前环境】
  只有场景终态，该轮之前没有环境回合时按空处理，否则用终态并在现场备注里写明；
- 记忆块（`memory_context_used` 没落盘）：`empty` 统一给空记忆块（默认，变体之间完全公平）；
  `checkpoint` 从前置快照的向量库副本检索（开场时的记忆，不含本场中途固化的；会调 embedding）。

结论只在变体之间相对比较，不当绝对值。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import re
import shutil
import statistics
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from backend.agents.character_agent import _ATTEMPT_RULE, CharacterAgent
from backend.config import settings
from backend.exceptions import LLMError, PlotSystemError
from backend.memory import MemoryManager
from backend.memory.long_term import LongTermMemory
from backend.models import (
    ActionStatus,
    CharacterState,
    DialogueTurn,
    EnvironmentMode,
    LLMUsageStat,
    Scene,
    SceneConfig,
    Snapshot,
)
from backend.scene_engine.continuation import trim_continuation
from backend.scene_engine.engine import SceneEngine
from backend.services import repository
from backend.snapshot import SnapshotManager
from backend.utils.db import init_db
from backend.utils.llm import estimate_tokens
from backend.utils.turns import is_character_turn, render_turn
from backend.utils.usage import UsageMeter, total, usage_scope

_DEFAULT_VARIANTS = ("base", "A", "B", "C", "A+B", "t0.5")
#: 一个回复里同一行出现这么多次就记为复读（只做描述，不参与判定）
_REPEAT_LINES = 3
_MEMORY_MODES = ("empty", "checkpoint")


# ---------------------------------------------------------------------------
# 现场
# ---------------------------------------------------------------------------


@dataclass
class ReplaySite:
    label: str
    scene: Scene
    turn: DialogueTurn
    speaker: CharacterAgent
    other_names: list[str]
    scene_context: dict
    extras: dict
    #: 该轮之前的全部轮次（含环境回合），按日志顺序
    prior: list[DialogueTurn]
    #: 基线视图的"目前对话"，与引擎逐行一致
    transcript: list[str]
    opening: str
    memory_context: list[str]
    mode: str
    notes: list[str] = field(default_factory=list)


def parse_site(spec: str) -> tuple[str, int]:
    scene, _, turn = spec.partition(":")
    if not scene.strip() or not turn.strip():
        raise ValueError(f"--site 格式应为 SCENE_ID:TURN（回放必须指向一个已发生的角色轮次）：{spec!r}")
    try:
        return scene.strip(), int(turn)
    except ValueError as exc:
        raise ValueError(f"--site 的轮次不是整数：{spec!r}") from exc


def infer_mode(scene: Scene) -> str:
    """档位没落盘，只能从痕迹推断：有环境回合或裁决过的动作就是 adjudicate。

    record 档与 off 档的角色 prompt 逐字相同，二者对回放没有区别。
    """
    adjudicated = {ActionStatus.PENDING.value, ActionStatus.RESOLVED.value, ActionStatus.FAILED.value}
    for turn in scene.dialogue_log:
        if not is_character_turn(turn):
            return EnvironmentMode.ADJUDICATE.value
        if any(a.status in adjudicated for a in turn.actions):
            return EnvironmentMode.ADJUDICATE.value
    if any(t.actions for t in scene.dialogue_log):
        return EnvironmentMode.RECORD.value
    return EnvironmentMode.OFF.value


def _find_scene(scenes: list[Scene], prefix: str) -> Scene:
    hits = [s for s in scenes if s.scene_id.startswith(prefix)]
    if not hits:
        raise ValueError(f"项目里没有以 {prefix} 开头的场景")
    if len(hits) > 1:
        raise ValueError(f"场景前缀 {prefix} 不唯一：{', '.join(s.scene_id[:12] for s in hits)}")
    return hits[0]


def _apply_state(card, state: CharacterState | None):
    # 与 orchestrator.build_character_agents 的回填一致；记忆层不连接（回放不碰向量库）
    if state is not None:
        card.current_emotion = state.current_emotion
        card.current_goal = state.current_goal
        card.current_location = state.current_location
        card.relationships = dict(state.relationships)
    return card


def _target_index(scene: Scene, turn_number: int) -> int:
    for i, turn in enumerate(scene.dialogue_log):
        if turn.turn_number == turn_number:
            if not is_character_turn(turn):
                raise ValueError(f"场景 {scene.scene_id[:8]} 第 {turn_number} 轮是环境回合，不能回放")
            return i
    raise ValueError(f"场景 {scene.scene_id[:8]} 没有第 {turn_number} 轮")


async def load_site(
    project_id: str,
    spec: str,
    *,
    scenes: list[Scene],
    mode_override: str | None = None,
    memory: str = "empty",
) -> ReplaySite:
    prefix, turn_number = parse_site(spec)
    scene = _find_scene(scenes, prefix)
    index = _target_index(scene, turn_number)
    target = scene.dialogue_log[index]
    prior = list(scene.dialogue_log[:index])
    notes: list[str] = []

    sm = SnapshotManager(project_id)
    snap: Snapshot | None = (
        await sm.get_snapshot(scene.snapshot_id_before) if scene.snapshot_id_before else None
    )
    if snap is None:
        notes.append("没有前置快照：世界变量按空、角色状态取当前角色卡")
    states = snap.character_states if snap else {}
    world = snap.world_state_variables if snap else {}

    agents: list[CharacterAgent] = []
    for cid in scene.participating_characters:
        card = _apply_state(await repository.get_character(project_id, cid), states.get(cid))
        agents.append(CharacterAgent(card, MemoryManager(cid, project_id, scene.branch_id)))
    speaker = next((a for a in agents if a.character_id == target.character_id), None)
    if speaker is None:
        raise ValueError(f"第 {target.turn_number} 轮的角色 {target.character_name} 不在本场参演名单里")

    mode = mode_override or infer_mode(scene)
    env_before = any(not is_character_turn(t) for t in prior)
    if env_before and scene.environment_state:
        notes.append("【当前环境】取场景终态，可能含该轮之后的变化")
    replay_scene = replace(scene, environment_state=dict(scene.environment_state) if env_before else {})

    objects = []
    if mode != EnvironmentMode.OFF.value and scene.objects_present:
        by_id = {o.object_id: o for o in await repository.list_objects(project_id)}
        missing = [oid for oid in scene.objects_present if oid not in by_id]
        if missing:
            notes.append(f"在场物件已不存在：{'、'.join(missing)}")
        objects = [by_id[oid] for oid in scene.objects_present if oid in by_id]
    if objects:
        notes.append("物件取当前文件，事后编辑过会漂移")

    config = SceneConfig(
        name=scene.name,
        description=scene.description,
        participating_characters=scene.participating_characters,
        location=scene.location,
        objects_present=list(scene.objects_present),
        initial_conditions=scene.initial_conditions,
        max_turns=scene.max_turns,
        speaker_mode=scene.speaker_mode,
        opening_narration=scene.initial_conditions.get("opening_narration", ""),
    )
    engine = SceneEngine(
        replay_scene, config, agents, sm,
        world_variables=world, objects=objects, environment_mode=mode,
        # 只为过构造校验，回放不裁决
        adjudicator=_NoAdjudicator() if mode == EnvironmentMode.ADJUDICATE.value else None,
    )
    scene_context = engine._scene_context()
    opening = config.opening_narration
    # 与 SceneEngine.run 开场一致：没有历史时先放开场白一行
    transcript = [f"【旁白】{opening}"] if opening else []
    transcript += [engine._turn_line(t) for t in prior]

    memory_context: list[str] = []
    if memory == "checkpoint":
        query = speaker.memory_query(scene_context, transcript)
        memory_context, note = await _checkpoint_memory(
            snap, project_id, speaker.character_id, scene.branch_id, query
        )
        if note:
            notes.append(note)

    return ReplaySite(
        label=f"{scene.scene_id[:8]}:{target.turn_number}",
        scene=scene,
        turn=target,
        speaker=speaker,
        other_names=[a.name for a in agents if a is not speaker],
        scene_context=scene_context,
        extras=engine._respond_extras(speaker),
        prior=prior,
        transcript=transcript,
        opening=opening,
        memory_context=memory_context,
        mode=mode,
        notes=notes,
    )


class _NoAdjudicator:
    async def adjudicate(self, *args, **kwargs):  # pragma: no cover - 回放从不裁决
        raise RuntimeError("回放评测不裁决动作")


async def _checkpoint_memory(
    snap: Snapshot | None, project_id: str, character_id: str, branch_id: str, query: str
) -> tuple[list[str], str]:
    """从前置快照的向量库副本检索开场时的记忆。

    用真实的 `LongTermMemory` 打开临时副本，承接规则全走生产代码：分叉初始化凭据封住的分支
    不回退项目级老集合，未承接的分支集合会把老集合并进来（工单08 I3、陷阱 12）。手写一份回退
    的话，这两条迟早漂移（30b 评审）。承接会往副本里写，副本用完即删；只打开副本是因为
    PersistentClient 打开即改写文件（I2）。
    """
    if snap is None or not snap.chroma_checkpoint:
        return [], "前置快照不含向量库，记忆块按空"
    ckpt = Path(snap.chroma_checkpoint)
    if not (ckpt / "chroma.sqlite3").exists():
        return [], "前置快照不含向量库，记忆块按空"

    work = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="plotsystem_replay_"))
    memory = LongTermMemory(character_id, project_id, branch_id)
    memory.db_dir = work / "ckpt"
    try:
        await asyncio.to_thread(shutil.copytree, ckpt, memory.db_dir)
        await memory.connect()
        if memory._collection is None:
            return [], "Chroma 不可用，记忆块按空"
        chunks = await memory.retrieve(query, settings.MEMORY_TOP_K)
        return [c.text for c in chunks], "" if chunks else "快照里该角色没有可检索的记忆"
    finally:
        SnapshotManager._close_chroma_client(memory._client, stop_fallback=True)
        await asyncio.to_thread(shutil.rmtree, work, True)


# ---------------------------------------------------------------------------
# 变体
# ---------------------------------------------------------------------------

Messages = list[dict]


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    build: Callable[[ReplaySite], Messages]
    temperature: float | None = None
    #: 本变体视图特有、`trim_continuation` 认不出的续写形态
    extra_marker: Callable[[ReplaySite], re.Pattern[str] | None] = lambda site: None


def _windowed(site: ReplaySite, lines: list[str]) -> CharacterAgent:
    """一个独立的角色实例，"目前对话"的窗口起点已推演到该轮之前。

    `_recent_transcript` 把起点记在实例上，只在超预算时成块前推、之后沿用；从 0 起一次算到底
    会比真实调用多丢一截（30b 评审）。所以按本角色此前每次发言时看到的行数依次推一遍。
    每个变体各用一份浅拷贝：起点是实例上唯一的可变状态，变体之间不能互相带。
    continue 每段都新建实例、起点归零，而段边界没落盘，这里只能从场景开头推起。
    """
    agent = copy.copy(site.speaker)
    agent._transcript_start = 0
    opening = 1 if site.opening else 0
    for i, turn in enumerate(site.prior):
        if is_character_turn(turn) and turn.character_id == site.speaker.character_id:
            agent._recent_transcript(lines[: opening + i])
    return agent


def build_base(site: ReplaySite) -> Messages:
    """真实的 `CharacterAgent.build_messages`，只有记忆块由口径决定。"""
    return _windowed(site, site.transcript).build_messages(
        site.scene_context, site.transcript, site.memory_context, **site.extras
    )


# ---- A：规则 + 沉浸 ----

_A_IDENTITY = "你此刻就身处这场戏里，只能凭自己的眼睛和耳朵知道周围发生了什么。\n"
_A_BOUNDARY = (
    "- 只写你自己这一轮：说完你的话、做完你的动作就停。不替其他角色写台词或动作，"
    "不写环境、旁白，也不输出任何【】标签\n"
    "- 别人怎么回应、周围发生了什么，要等你这一轮说完之后才会知道\n"
)
_A_ATTEMPT = "- 对物件的动作只写你的尝试，不写结果：结果在你说完之后才会知道，不归你写\n"
_FORMAT_ANCHOR = "- 保持角色一致性"


def _instruction(name: str) -> str:
    return f"现在轮到你（{name}）发言，请按行为格式规范回应。"


def _a_instruction(name: str) -> str:
    return f"现在轮到你（{name}）。按行为格式规范写出你这一轮的言行，写完就停。"


def _swap(text: str, old: str, new: str, what: str) -> str:
    if old not in text:
        # 基线模板改了而这里没跟上：比较的就不再是"基线 + 一处改动"，宁可停下
        raise ValueError(f"变体 A 的锚点「{what}」在基线 prompt 里找不到，先更新脚本")
    return text.replace(old, new, 1)


def apply_a(site: ReplaySite, messages: Messages) -> Messages:
    out = [dict(m) for m in messages]
    system = out[0]["content"]
    first_line = f"你是【{site.speaker.name}】。\n"
    system = _swap(system, first_line, first_line + _A_IDENTITY, "身份行")
    if _ATTEMPT_RULE in system:
        system = system.replace(_ATTEMPT_RULE, _A_ATTEMPT, 1)
    system = _swap(system, _FORMAT_ANCHOR, _A_BOUNDARY + _FORMAT_ANCHOR, "行为格式规范")
    out[0]["content"] = system
    last = out[-1]
    last["content"] = _swap(
        last["content"], _instruction(site.speaker.name), _a_instruction(site.speaker.name), "发言指令"
    )
    return out


# ---- B：叙事化视图 ----

_B_NARRATION = "※ "
_STORED_LINE_RE = re.compile(r"^(?P<who>[^:\n]{1,32}): (?P<rest>.*)$", re.DOTALL)
_STAR_RE = re.compile(r"\*(.+?)\*", re.DOTALL)
_BRACKET_RE = re.compile(r"\[(.*?)\]", re.DOTALL)
_ENV_MEMORY_RE = re.compile(r"^【[^】]*】(?P<text>.*?)(?:（私密：(?P<private>.*)）)?$", re.DOTALL)


def _b_turn(turn: DialogueTurn, self_id: str) -> str:
    """不与发言人行、区块标题同形：本人记作"你"，环境与开场是不带名字的叙述行。"""
    if not is_character_turn(turn):
        return f"{_B_NARRATION}{turn.narration or ''}".strip()
    who = "你" if turn.character_id == self_id else turn.character_name
    line = who
    if turn.action:
        line += f"（{turn.action}）"
    if turn.dialogue:
        line += f"：「{turn.dialogue}」"
    elif turn.action:
        line += "。"
    return line


def b_transcript(site: ReplaySite) -> list[str]:
    lines = [f"{_B_NARRATION}{site.opening}"] if site.opening else []
    return lines + [_b_turn(t, site.speaker.character_id) for t in site.prior]


def b_memory(site: ReplaySite) -> list[str]:
    """记忆是存储格式的原文（剧本行），转述成"你记得…"。只改视图，不动存储（R1）。"""
    out = []
    for text in site.memory_context:
        env = _ENV_MEMORY_RE.match(text) if text.startswith("【") else None
        if env:
            line = f"你记得当时{env.group('text').strip()}"
            if env.group("private"):
                line += f"（只有你察觉到：{env.group('private').strip()}）"
            out.append(line)
            continue
        m = _STORED_LINE_RE.match(text)
        if not m:
            out.append(f"你记得：{text}")
            continue
        who = "你自己" if m.group("who") == site.speaker.name else m.group("who")
        rest = m.group("rest")
        thoughts = [t.strip() for t in _BRACKET_RE.findall(rest) if t.strip()]
        rest = _BRACKET_RE.sub("", rest)
        actions = [a.strip() for a in _STAR_RE.findall(rest) if a.strip()]
        words = re.sub(r"\s+", " ", _STAR_RE.sub("", rest)).strip()
        line = f"你记得{who}"
        if actions:
            line += f"（{'；'.join(actions)}）"
        line += f"说过「{words}」" if words else "这样做过"
        if thoughts:
            line += f"，当时你心想：{'；'.join(thoughts)}"
        out.append(line)
    return out


def build_b(site: ReplaySite) -> Messages:
    lines = b_transcript(site)
    return _windowed(site, lines).build_messages(
        site.scene_context, lines, b_memory(site), **site.extras
    )


def b_marker(site: ReplaySite) -> re.Pattern[str]:
    names = sorted({site.speaker.name, *site.other_names, "你"}, key=len, reverse=True)
    alt = "|".join(re.escape(n) for n in names)
    # 照抄 B 视图续写：叙述行，或"名字（动作）：「"——带括号时名字与冒号不相邻，30a 认不出
    return re.compile(
        rf"(?:^|(?<=[\s。！？!?…」”』）]))(?:※|(?:{alt})(?:（[^）]*）)?\s*[:：]\s*「)",
        re.MULTILINE,
    )


# ---- C：多轮消息结构 ----


def _own_reply(turn: DialogueTurn) -> str:
    # 本人的过去轮次按回复格式还给它，可带自己的独白（R3）
    parts = []
    if turn.action:
        parts.append(f"*{turn.action}*")
    if turn.dialogue:
        parts.append(turn.dialogue)
    if turn.inner_thought:
        parts.append(f"[{turn.inner_thought}]")
    return " ".join(parts) or "……"


def build_c(site: ReplaySite) -> Messages:
    """本人过去的轮次作 assistant，其余（他人、环境、开场白）作 user，块与指令在末条 user。

    system 与他人行的写法都与基线相同，差别只在消息结构。原型不做预算裁剪：历史超出
    `TRANSCRIPT_TOKEN_BUDGET` 的现场，C 看到的比基线多，比较前先看现场的 prompt 规模。
    """
    base = build_base(site)
    # 末条 user 的块（当前环境 / 想起的 / 指令）用真实的 prompt_tail 现拼，不从基线里切：
    # 历史台词里可能有空行，被污染的台词里还可能有区块标题字样（30b 评审）
    blocks = site.speaker.prompt_tail(site.memory_context, site.extras.get("environment", ""))
    engine_lines: list[tuple[str, str]] = []
    if site.opening:
        engine_lines.append(("user", f"【旁白】{site.opening}"))
    for turn in site.prior:
        if is_character_turn(turn) and turn.character_id == site.speaker.character_id:
            engine_lines.append(("assistant", _own_reply(turn)))
        else:
            engine_lines.append(("user", SceneEngine._turn_line(turn)))

    messages: Messages = [base[0]]
    for role, text in engine_lines:
        if len(messages) > 1 and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + text
        else:
            messages.append({"role": role, "content": text})
    if len(messages) > 1 and messages[-1]["role"] == "user":
        messages[-1]["content"] += "\n\n" + blocks
    else:
        if len(messages) == 1:
            blocks = "（场景刚刚开始）\n\n" + blocks
        messages.append({"role": "user", "content": blocks})
    return messages


VARIANTS: dict[str, Variant] = {
    "base": Variant("base", "现行 prompt（真实的 build_messages）", build_base),
    "A": Variant("A", "规则 + 沉浸：输出边界、第一人称处境、R7 改写", lambda s: apply_a(s, build_base(s))),
    "B": Variant("B", "叙事化视图：`名字（动作）：「台词」`、本人记作你、环境与开场为 ※ 叙述行",
                 build_b, extra_marker=b_marker),
    "C": Variant("C", "多轮消息：本人过去轮次作 assistant", build_c),
    "A+B": Variant("A+B", "A 的规则叠在 B 的视图上", lambda s: apply_a(s, build_b(s)), extra_marker=b_marker),
    "A+C": Variant("A+C", "A 的规则叠在 C 的结构上", lambda s: apply_a(s, build_c(s))),
    "t0.5": Variant("t0.5", "现行 prompt，温度 0.5（参数对照，不是修复）", build_base, temperature=0.5),
}


# ---------------------------------------------------------------------------
# 运行与统计
# ---------------------------------------------------------------------------


@dataclass
class Sample:
    variant: str
    site: str
    index: int
    raw: str = ""
    kept: str = ""
    cut: str = ""
    error: str = ""
    empty: bool = False
    #: 变体特有形态在保留部分里的位置（-1 = 没有）
    extra_at: int = -1
    repeated: int = 0

    @property
    def polluted(self) -> bool:
        return bool(self.cut)


def judge(site: ReplaySite, variant: Variant, raw: str, index: int) -> Sample:
    kept, cut = trim_continuation(raw, self_name=site.speaker.name, other_names=site.other_names)
    actions, dialogue, thoughts = SceneEngine._split_reply(kept)
    empty = not (dialogue or any(a.strip() for a in actions) or any(t.strip() for t in thoughts))
    marker = variant.extra_marker(site)
    hit = marker.search(kept) if marker is not None else None
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    repeated = max(Counter(lines).values(), default=0)
    return Sample(
        variant=variant.name, site=site.label, index=index, raw=raw, kept=kept, cut=cut,
        empty=empty, extra_at=hit.start() if hit else -1,
        repeated=repeated if repeated >= _REPEAT_LINES else 0,
    )


def planned_calls(sites: list[ReplaySite], variants: list[Variant], n: int) -> int:
    return len(sites) * len(variants) * n


async def run_replay(
    sites: list[ReplaySite], variants: list[Variant], n: int, concurrency: int = 4
) -> tuple[list[Sample], dict[str, UsageMeter]]:
    meters = {v.name: UsageMeter() for v in variants}
    gate = asyncio.Semaphore(max(1, concurrency))
    # 消息先全部建好，调用期间不再碰角色实例
    plans = [(site, v, v.build(site)) for site in sites for v in variants]

    async def one(site: ReplaySite, variant: Variant, messages: Messages, index: int) -> Sample:
        async with gate:
            with usage_scope(meters[variant.name]):
                try:
                    raw = await site.speaker.complete(messages, temperature=variant.temperature)
                except LLMError as exc:
                    return Sample(variant=variant.name, site=site.label, index=index, error=str(exc))
        return judge(site, variant, raw, index)

    samples = await asyncio.gather(
        *(one(site, v, msgs, i) for site, v, msgs in plans for i in range(n))
    )
    return list(samples), meters


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class VariantStats:
    variant: str
    samples: int = 0
    answered: int = 0
    polluted: int = 0
    extra: int = 0
    empty: int = 0
    failed: int = 0
    repeated: int = 0
    raw_median: float = 0.0
    raw_p90: float = 0.0
    kept_median: float = 0.0
    cut_heads: Counter = field(default_factory=Counter)
    usage: LLMUsageStat = field(default_factory=LLMUsageStat)


def _cut_head(cut: str) -> str:
    """截掉部分以什么开头（`【环境】` / `阿德里安:` …），只做描述。"""
    head = cut.lstrip()
    m = re.match(r"【[^】]{0,16}】|[^\s:：]{1,16}\s*[:：]", head)
    return m.group(0).replace(" ", "") if m else head[:8]


def _p90(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)])


def summarize(samples: list[Sample], meters: dict[str, UsageMeter], variants: list[Variant]) -> list[VariantStats]:
    out = []
    for v in variants:
        own = [s for s in samples if s.variant == v.name]
        ok = [s for s in own if not s.error]
        raw_lens = [len(s.raw) for s in ok]
        kept_lens = [len(s.kept) for s in ok]
        stats = VariantStats(
            variant=v.name,
            samples=len(own),
            answered=len(ok),
            polluted=sum(s.polluted for s in ok),
            extra=sum(s.extra_at >= 0 for s in ok),
            empty=sum(s.empty for s in ok),
            failed=len(own) - len(ok),
            repeated=sum(bool(s.repeated) for s in ok),
            raw_median=statistics.median(raw_lens) if raw_lens else 0.0,
            raw_p90=_p90(raw_lens),
            kept_median=statistics.median(kept_lens) if kept_lens else 0.0,
            cut_heads=Counter(_cut_head(s.cut) for s in ok if s.cut),
            usage=total(meters[v.name].snapshot()) if v.name in meters else LLMUsageStat(),
        )
        out.append(stats)
    return out


def _pct(k: int, n: int) -> str:
    if not n:
        return "—"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {k / n:.0%}（{lo:.0%}–{hi:.0%}）"


def stats_table(stats: list[VariantStats]) -> str:
    rows = [
        "| 变体 | 污染（95% 区间） | 变体特有形态 | 空回复 | 调用失败 | 重试 | 复读 | 原始长度 中位 / P90 | 保留长度中位 | 输出 token | 截掉部分开头 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in stats:
        heads = "、".join(f"{h}×{c}" for h, c in s.cut_heads.most_common(4)) or "—"
        tokens = f"{'≈' if s.usage.estimated_calls else ''}{s.usage.completion_tokens}"
        rows.append(
            f"| {s.variant} | {_pct(s.polluted, s.answered)} | {s.extra} | {s.empty} | {s.failed} | "
            f"{s.usage.retries} | {s.repeated} | {s.raw_median:.0f} / {s.raw_p90:.0f} | {s.kept_median:.0f} | "
            f"{tokens} | {heads} |"
        )
    return "\n".join(rows)


def _site_stats_table(samples: list[Sample], sites: list[ReplaySite], variants: list[Variant]) -> str:
    header = "| 现场 | " + " | ".join(v.name for v in variants) + " |"
    rows = [header, "|---|" + "---|" * len(variants)]
    for site in sites:
        cells = []
        for v in variants:
            own = [s for s in samples if s.site == site.label and s.variant == v.name and not s.error]
            cells.append(f"{sum(s.polluted for s in own)}/{len(own)}")
        rows.append(f"| {site.label} {site.turn.character_name} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _method_notes(memory: str) -> list[str]:
    return [
        f"- 记忆块：{'空记忆块（所有变体相同）' if memory == 'empty' else '前置快照的向量库检索（开场时的记忆）'}",
        "- 世界变量与角色时点状态取前置快照；人设取当前角色卡",
        "- \"目前对话\" = 开场白一行 + 该轮之前的全部轮次（若该轮属于 continue 段，真实现场没有开场白）",
        "- 污染 = `trim_continuation` 截掉了内容；变体特有形态单列，不并入污染率；无标记的续写只能人读",
        "- 只在变体之间相对比较，不当绝对值",
    ]


def _quote(text: str, limit: int | None = None) -> str:
    text = text if limit is None or len(text) <= limit else text[:limit] + f"……（共 {len(text)} 字）"
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines() or [""])


def render_markdown(
    sites: list[ReplaySite], variants: list[Variant], samples: list[Sample],
    stats: list[VariantStats], *, memory: str, n: int, model: str,
) -> str:
    out = [
        f"# 角色回复回放评测（工单30b）{datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"角色模型 `{model}`，每个现场每个变体 {n} 次，输出上限 `CHARACTER_MAX_TOKENS={settings.CHARACTER_MAX_TOKENS}`。",
        "",
        "## 口径",
        *_method_notes(memory),
        "",
        "## 变体",
        *(f"- **{v.name}**：{v.description}" for v in variants),
        "",
        "## 汇总",
        stats_table(stats),
        "",
        "### 各现场污染次数",
        _site_stats_table(samples, sites, variants),
        "",
    ]
    for site in sites:
        out += [
            f"## 现场 {site.label} · {site.scene.name} · 第 {site.turn.turn_number} 轮 {site.turn.character_name}",
            "",
            f"档位 `{site.mode}`，此前 {len(site.prior)} 轮，记忆块 {len(site.memory_context)} 条。"
            + (f" 备注：{'；'.join(site.notes)}" if site.notes else ""),
            "",
            "**当时落库的回复**：",
            _quote(render_turn(site.turn, inner_thought=True), 400),
            "",
        ]
        for v in variants:
            out += [f"### {v.name}", ""]
            for s in [x for x in samples if x.site == site.label and x.variant == v.name]:
                if s.error:
                    out += [f"{s.index + 1}. **调用失败**：{s.error}", ""]
                    continue
                tags = []
                if s.polluted:
                    tags.append(f"截掉 {len(s.cut)} 字，开头「{_cut_head(s.cut)}」")
                if s.extra_at >= 0:
                    tags.append(f"变体特有形态 @ {s.extra_at}")
                if s.empty:
                    tags.append("截后为空")
                if s.repeated:
                    tags.append(f"同一行重复 {s.repeated} 次")
                out += [
                    f"{s.index + 1}. 原始 {len(s.raw)} 字" + (f"；{'；'.join(tags)}" if tags else ""),
                    "",
                    _quote(s.kept or "（空）"),
                    "",
                ]
                if s.cut:
                    out += ["   截掉的部分：", "", _quote(s.cut, 300), ""]
    return "\n".join(out)


def render_json(
    sites: list[ReplaySite], variants: list[Variant], samples: list[Sample],
    stats: list[VariantStats], *, memory: str, n: int, model: str,
) -> str:
    return json.dumps(
        {
            "model": model,
            "n": n,
            "memory": memory,
            "variants": [{"name": v.name, "description": v.description} for v in variants],
            "sites": [
                {
                    "label": s.label, "scene_id": s.scene.scene_id, "scene_name": s.scene.name,
                    "turn": s.turn.turn_number, "speaker": s.turn.character_name, "mode": s.mode,
                    "prior_turns": len(s.prior), "memory_items": len(s.memory_context), "notes": s.notes,
                }
                for s in sites
            ],
            "stats": [
                {**{k: v for k, v in asdict(s).items() if k not in ("cut_heads", "usage")},
                 "cut_heads": dict(s.cut_heads), "usage": asdict(s.usage)}
                for s in stats
            ],
            "samples": [asdict(s) for s in samples],
        },
        ensure_ascii=False,
        indent=2,
    )


def _exclusive_write(directory: Path, stem: str, suffix: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        path = directory / f"{stem}_{uuid4().hex[:8]}{suffix}"
        try:
            # 排他创建：同一秒内的两次运行不能互相覆盖（判定有随机性，每次结果都要留）
            with open(path, "x", encoding="utf-8") as f:
                f.write(text)
            return path
        except FileExistsError:
            continue
    raise RuntimeError("连续 5 次随机后缀都撞上已存在的文件名，已放弃写入")


def preview(sites: list[ReplaySite], variants: list[Variant], n: int, memory: str) -> str:
    lines = ["回放现场："]
    for site in sites:
        lines.append(
            f"  {site.label} 「{site.scene.name}」第 {site.turn.turn_number} 轮 {site.turn.character_name}"
            f"（档位 {site.mode}，此前 {len(site.prior)} 轮，记忆块 {len(site.memory_context)} 条）"
        )
        for note in site.notes:
            lines.append(f"    备注：{note}")
    lines.append("\n变体（每次调用的 prompt token 估算，按现场合计）：")
    for v in variants:
        tokens = sum(estimate_tokens(m["content"]) for site in sites for m in v.build(site))
        temp = f"，温度 {v.temperature}" if v.temperature is not None else ""
        lines.append(f"  {v.name}：{v.description}（≈{tokens} token{temp}）")
    lines.append(f"\n记忆块口径：{memory}")
    lines.append(
        f"将发起 {planned_calls(sites, variants, n)} 次角色调用"
        f"（{len(sites)} 个现场 × {len(variants)} 个变体 × {n} 次），模型 {settings.character_model}"
    )
    return "\n".join(lines)


async def _main(args: argparse.Namespace) -> int:
    await init_db()
    try:
        await repository.get_project(args.project)
    except PlotSystemError as exc:
        print(f"未找到项目 {args.project}：{exc}")
        return 1
    try:
        variants = [VARIANTS[name.strip()] for name in args.variants.split(",") if name.strip()]
    except KeyError as exc:
        print(f"未知变体 {exc}，可选：{', '.join(VARIANTS)}")
        return 1
    scenes = await repository.list_scenes(args.project)
    try:
        sites = [
            await load_site(args.project, spec, scenes=scenes, mode_override=args.mode, memory=args.memory)
            for spec in args.site
        ]
        print(preview(sites, variants, args.n, args.memory))
    except ValueError as exc:
        print(f"现场重建失败：{exc}")
        return 1
    if not args.run:
        print("\n以上为预览，没有调用 LLM。确认后加 --run 执行。")
        return 0

    samples, meters = await run_replay(sites, variants, args.n, args.concurrency)
    stats = summarize(samples, meters, variants)
    print("\n" + stats_table(stats))
    print("\n" + _site_stats_table(samples, sites, variants))

    out_dir = Path(args.out) if args.out else settings.project_dir(args.project)
    stem = f"replay_{datetime.now():%Y%m%d-%H%M%S}"
    common = dict(memory=args.memory, n=args.n, model=settings.character_model)
    md = _exclusive_write(out_dir, stem, ".md", render_markdown(sites, variants, samples, stats, **common))
    data = _exclusive_write(out_dir, stem, ".json", render_json(sites, variants, samples, stats, **common))
    print(f"\n样本：{md}\n原始数据：{data}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="角色回复的回放评测（工单30b，调 LLM）")
    parser.add_argument("--project", required=True, help="project_id")
    parser.add_argument("--site", action="append", required=True, help="SCENE_ID:TURN，可重复；场景 ID 可写前缀")
    parser.add_argument("--variants", default=",".join(_DEFAULT_VARIANTS), help=f"可选：{', '.join(VARIANTS)}")
    parser.add_argument("-n", type=int, default=5, help="每个现场每个变体生成几次")
    parser.add_argument("--memory", choices=_MEMORY_MODES, default="empty", help="记忆块口径")
    parser.add_argument("--mode", choices=[m.value for m in EnvironmentMode], default=None, help="覆盖推断的档位")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--run", action="store_true", help="真正调用 LLM（默认只预览）")
    parser.add_argument("--out", default=None, help="输出目录，缺省为项目目录")
    args = parser.parse_args()
    if args.n < 1:
        parser.error("-n 至少为 1")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
