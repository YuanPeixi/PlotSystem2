"""发言者选择策略（工单11）。

selector 模式采用**独立评分**：对每个候选角色各发起一次轻量 LLM 调用，
打分器一次只看一个角色，因而不存在"候选列表位置偏见"（旧实现把所有名字
排成一列问 LLM，模型系统性偏向列表首项）；N 次调用用 asyncio.gather 并行，
整轮延迟仍是一个 RTT。打分结果再叠加两个纯本地信号：被点名加分、重复发言
惩罚，最后取 argmax（SELECTOR_TEMPERATURE > 0 时改为 softmax 采样）。

【契约1】打分 prompt 只允许使用角色的 known_facts 与公共对话文本，
绝不能出现 unknown_facts，也不能出现任何角色的 inner_thought
（transcript 由 SceneEngine._turn_line() 生成，本身已剥离内心独白）。

【契约3】每个角色的打分 system 前缀整场不变，"目前对话"只追加不滑窗、
超预算时成块丢弃，与 CharacterAgent 的 prompt 结构约定保持一致。
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
from dataclasses import dataclass, field
from statistics import median

from backend.agents.character_agent import CharacterAgent
from backend.config import settings
from backend.models import DialogueTurn
from backend.utils.llm import chat_safe, estimate_tokens
from backend.utils.logger import get_logger

logger = get_logger("scene_engine.selector")

# 被点名信号的回溯轮数：只看最近两轮，更早的称呼已经被回应过了
_ADDRESSED_LOOKBACK = 2
_SCORE_MAX_TOKENS = 200

_SCORE_SYSTEM = """你是一个"发言意愿评估器"。你要评估指定角色在当前对话情境下开口发言的意愿强度。
你不是在扮演这个角色，也不要替他生成台词，只做评分。

【被评估角色】{name}
【角色设定】{persona}
【说话风格】{speech_style}
【当前状态】情绪：{current_emotion}；目标：{current_goal}
【他已知的事实】
{known_facts}

【评分维度】（均为 0-10 的整数）
- urge：发言欲望。他此刻的情绪、目标是否被对话触动，有多想开口。
- relevance：相关度。最近的对话是否与他有关、是否在谈论他关心的人或事。
- initiative：主动性。基于性格设定，他在这种场合是否倾向主动开口（内敛/寡言的角色应偏低）。

【输出格式】只输出一个 JSON 对象，不要任何解释文字或代码块标记：
{{"urge": 0, "relevance": 0, "initiative": 0, "reason": "不超过20字的理由"}}"""


@dataclass
class SpeakerScore:
    """单个候选角色的评分明细，用于日志与调试。"""

    character_id: str
    name: str
    urge: float = 0.0
    relevance: float = 0.0
    initiative: float = 0.0
    addressed: bool = False
    penalty: float = 0.0
    total: float = 0.0
    reason: str = ""
    scored_by_llm: bool = False  # False 表示该角色的 LLM 打分失败、走了兜底值

    def brief(self) -> str:
        flag = "" if self.scored_by_llm else "!"
        at = "@" if self.addressed else ""
        return (
            f"{self.name}{flag}={self.total:.1f}"
            f"(u{self.urge:.0f} r{self.relevance:.0f} i{self.initiative:.0f}"
            f" {at}-{self.penalty:.2f})"
        )


@dataclass
class SelectionTrace:
    """一次选人的完整轨迹。兜底必须可观测，不允许静默回退。"""

    chosen_id: str = ""
    scores: list[SpeakerScore] = field(default_factory=list)
    llm_failures: int = 0
    degraded: bool = False  # 全部候选打分失败，退化为纯本地打分


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    else:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _clamp_score(value: object) -> float:
    try:
        return max(0.0, min(10.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def detect_addressed(text: str, names: dict[str, str]) -> set[str]:
    """找出文本中被直呼其名的角色 ID。

    按名字长度降序匹配并把命中部分掩掉，实现最长优先——否则"李明"会被
    "李明远"误命中（中文没有词边界，不能用 \\b）。
    """
    hits: set[str] = set()
    if not text:
        return hits
    remaining = text
    for cid, name in sorted(names.items(), key=lambda kv: len(kv[1]), reverse=True):
        if name and name in remaining:
            hits.add(cid)
            remaining = remaining.replace(name, "\x00" * len(name))
    return hits


class ScoringSpeakerSelector:
    """独立评分选择器。每场景一个实例（持有 transcript 窗口起点）。"""

    def __init__(self, agents: list[CharacterAgent]):
        self.agents = agents
        self._names = {a.character_id: a.name for a in agents}
        self._systems = {a.character_id: self._build_system(a) for a in agents}
        # 与 CharacterAgent._transcript_start 同理：成块前推而非逐行滑动
        self._transcript_start = 0

    async def select(
        self, transcript: list[str], turns: list[DialogueTurn]
    ) -> tuple[CharacterAgent, SelectionTrace]:
        """选出下一个发言者，并返回可观测的评分轨迹。"""
        addressed = self._detect_addressed_ids(turns)
        penalties = self._repeat_penalties(turns)
        user_msg = self._build_user(transcript)

        raw_scores = await asyncio.gather(
            *(self._score_one(agent, user_msg) for agent in self.agents)
        )
        trace = self._combine(raw_scores, addressed, penalties)
        chosen = self._pick(trace)
        trace.chosen_id = chosen.character_id

        detail = " ".join(s.brief() for s in trace.scores)
        if trace.degraded:
            logger.warning(
                "[selector] 全部候选打分失败，退化为纯本地打分 → 选中 %s｜%s",
                chosen.name,
                detail,
            )
        else:
            if trace.llm_failures:
                logger.warning(
                    "[selector] %d/%d 个候选打分失败，已用中位数兜底（带 ! 标记）",
                    trace.llm_failures,
                    len(self.agents),
                )
            logger.info("[selector] 选中 %s｜%s", chosen.name, detail)
        return chosen, trace

    # ---- prompt 构建 ----
    @staticmethod
    def _build_system(agent: CharacterAgent) -> str:
        card = agent.card
        # 契约1：这里只能取 known_facts，unknown_facts 是导演专属
        known = "\n".join(f"- {f}" for f in card.known_facts) or "（无特别已知事实）"
        return _SCORE_SYSTEM.format(
            name=card.name,
            persona=(card.persona or "（待补充）"),
            speech_style=card.speech_style or "（自然）",
            current_emotion=card.current_emotion,
            current_goal=card.current_goal or "（顺其自然）",
            known_facts=known,
        )

    def _build_user(self, transcript: list[str]) -> str:
        if not transcript:
            return "【目前对话】\n（场景刚刚开始）\n\n请输出 JSON 评分。"

        start = self._transcript_start
        budget = settings.SELECTOR_TRANSCRIPT_BUDGET
        if budget > 0:
            total = sum(estimate_tokens(line) for line in transcript[start:])
            if total > budget:
                target = int(budget * 0.75)  # 丢到水位线，留出后续若干轮的增长空间
                while start < len(transcript) - 1 and total > target:
                    total -= estimate_tokens(transcript[start])
                    start += 1
        self._transcript_start = start

        body = "\n".join(transcript[start:])
        if start > 0:
            body = f"（更早的 {start} 轮已省略）\n{body}"
        return f"【目前对话】\n{body}\n\n请输出 JSON 评分。"

    # ---- 打分 ----
    async def _score_one(self, agent: CharacterAgent, user_msg: str) -> SpeakerScore:
        score = SpeakerScore(character_id=agent.character_id, name=agent.name)
        messages = [
            {"role": "system", "content": self._systems[agent.character_id]},
            {"role": "user", "content": user_msg},
        ]
        try:
            raw = await chat_safe(
                messages,
                temperature=settings.SELECTOR_SCORE_TEMPERATURE,
                model=settings.selector_model,
                max_tokens=_SCORE_MAX_TOKENS,
                base_url=settings.selector_base_url,
                api_key=settings.selector_api_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[selector] %s 打分调用失败：%s", agent.name, exc)
            return score

        data = _extract_json(raw)
        if not data:
            logger.warning("[selector] %s 打分输出无法解析为 JSON：%.80s", agent.name, raw)
            return score

        score.urge = _clamp_score(data.get("urge"))
        score.relevance = _clamp_score(data.get("relevance"))
        score.initiative = _clamp_score(data.get("initiative"))
        score.reason = str(data.get("reason", ""))[:40]
        score.scored_by_llm = True
        return score

    def _combine(
        self,
        scores: list[SpeakerScore],
        addressed: set[str],
        penalties: dict[str, float],
    ) -> SelectionTrace:
        ok = [s for s in scores if s.scored_by_llm]
        trace = SelectionTrace(scores=scores, llm_failures=len(scores) - len(ok))
        trace.degraded = not ok

        # 打分失败的角色取成功者的中位数，避免被 0 分永久排除在候选之外
        if ok:
            fallback = (
                median(s.urge for s in ok),
                median(s.relevance for s in ok),
                median(s.initiative for s in ok),
            )
            for s in scores:
                if not s.scored_by_llm:
                    s.urge, s.relevance, s.initiative = fallback

        for s in scores:
            s.addressed = s.character_id in addressed
            s.penalty = penalties.get(s.character_id, 0.0)
            s.total = (
                settings.SELECTOR_WEIGHT_URGE * s.urge
                + settings.SELECTOR_WEIGHT_RELEVANCE * s.relevance
                + settings.SELECTOR_WEIGHT_INITIATIVE * s.initiative
                + settings.SELECTOR_ADDRESSED_BONUS * (1.0 if s.addressed else 0.0)
                - settings.SELECTOR_REPEAT_PENALTY * s.penalty
            )
        return trace

    def _pick(self, trace: SelectionTrace) -> CharacterAgent:
        by_id = {a.character_id: a for a in self.agents}
        temperature = settings.SELECTOR_TEMPERATURE
        if temperature > 0:
            top = max(s.total for s in trace.scores)
            weights = [math.exp((s.total - top) / temperature) for s in trace.scores]
            picked = random.choices(trace.scores, weights=weights, k=1)[0]
        else:
            picked = max(trace.scores, key=lambda s: s.total)
        return by_id[picked.character_id]

    # ---- 本地信号 ----
    def _detect_addressed_ids(self, turns: list[DialogueTurn]) -> set[str]:
        """最近若干轮中被别人直呼其名的角色。发言者不会因提到自己而加分。"""
        hits: set[str] = set()
        for turn in turns[-_ADDRESSED_LOOKBACK:]:
            text = " ".join(p for p in (turn.dialogue, turn.action) if p)
            for cid in detect_addressed(text, self._names):
                if cid != turn.character_id:
                    hits.add(cid)
        return hits

    def _repeat_penalties(self, turns: list[DialogueTurn]) -> dict[str, float]:
        """几何衰减的重复发言惩罚：刚说完压得最狠，但不硬性禁止连续发言。"""
        decay = settings.SELECTOR_PENALTY_DECAY
        next_index = len(turns)
        penalties: dict[str, float] = {}
        for idx, turn in enumerate(turns):
            penalties[turn.character_id] = penalties.get(turn.character_id, 0.0) + (
                decay ** (next_index - idx)
            )
        return penalties
