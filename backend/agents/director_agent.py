"""DirectorAgent：导演智能体。

唯一拥有全局知识图谱访问权、可查看所有角色内部状态的实体。
负责场景规划、模拟后评估、决策（继续/下一场/回滚）。
temperature 默认 0.3（追求一致性）。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter

from backend.config import settings
from backend.knowledge_graph import GraphManager
from backend.models import (
    MAX_UNRESOLVED_THREADS,
    PROGRESS_UNAVAILABLE,
    CharacterCard,
    CharacterState,
    DecisionType,
    DialogueTurn,
    DirectorDecision,
    Scene,
    SceneConfig,
    SceneEvaluation,
    goal_revision,
)
from backend.services import inspection
from backend.utils.context import TAIL_ONLY, ContextBudget, compact_lines, fit_lines
from backend.utils.llm import chat_safe, estimate_tokens
from backend.utils.logger import get_logger

logger = get_logger("agents.director")

#: 评分为该值表示"评估未生成"，而不是"得分很低"
SCORE_UNAVAILABLE = -1.0

#: 结局判定用的前情提要预算。梯概每条 50-100 字，够装下十几场；
#: 它不该与对白预算一样大，否则长线推演下整个评估请求会被历史吃掉。
_HISTORY_BUDGET_TOKENS = 2000

#: 未收束线索的总预算与单条上限。只限条数是不够的：它会被落库并逐场回喂，
#: 20 条超长线索一旦写进去，后续每一次规划与评估都拖着它（PR #17：预算是硬约束）。
_THREADS_BUDGET_TOKENS = 800
_THREAD_ITEM_TOKENS = 60


def unavailable_evaluation(scene_id: str) -> SceneEvaluation:
    """构造一份显式标记为"不可用"的评估。

    调用方拿不到评估时必须用它，而不是裸的 ``SceneEvaluation()``：后者四项分数
    默认 0.0，会直接撞进 make_decision 的阈值规则变成回滚。
    """
    return SceneEvaluation(
        scene_id=scene_id,
        narrative_goal_score=SCORE_UNAVAILABLE,
        dramatic_tension_score=SCORE_UNAVAILABLE,
        plot_deviation_score=SCORE_UNAVAILABLE,
        character_consistency_score=SCORE_UNAVAILABLE,
        story_progress=PROGRESS_UNAVAILABLE,
        story_progress_raw=PROGRESS_UNAVAILABLE,
        recommended_decision=DecisionType.NEXT_SCENE.value,
    )


def is_evaluation_unavailable(evaluation: SceneEvaluation) -> bool:
    """整份评估是否不可用。

    故意不把 ``story_progress`` 纳入：它只是其中一项度量，LLM 漏返回这一个键
    不应让四项正常分数整体作废、连带把决策打成保守默认。它自己的不可用
    由 ``PROGRESS_UNAVAILABLE`` 单独表达。
    """
    return min(
        evaluation.narrative_goal_score,
        evaluation.dramatic_tension_score,
        evaluation.plot_deviation_score,
        evaluation.character_consistency_score,
    ) < 0


def _extract_json(raw: str) -> tuple[dict, bool]:
    """从 LLM 输出里抠 JSON。返回 ``(data, ok)``。

    ok=False 必须被调用方区分对待：旧实现直接返回 ``{}``，让解析失败伪装成
    一份分数全默认的"正常评估"，无任何告警。
    """
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
        return {}, False
    return (data, True) if isinstance(data, dict) else ({}, False)


def _parse_number(value) -> float | None:
    """解析成有限浮点数，否则 None。

    两个坑都会静默变成满分/满进度：先钳后判的 ``max(0, min(1, nan))`` 返回上界，
    而 ``json.loads`` 默认就接受裸的 ``NaN`` / ``Infinity``；``float(True)`` 也是 1.0。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_progress(value) -> float:
    """把 LLM 给的推进度收进 0-1，无法解析时返回 PROGRESS_UNAVAILABLE。"""
    parsed = _parse_number(value)
    return PROGRESS_UNAVAILABLE if parsed is None else max(0.0, min(1.0, parsed))


def _parse_bool(value) -> bool | None:
    """严格布尔解析，无法判定时返回 None。

    不能直接用 ``bool()``：LLM 完全可能输出合法 JSON 的字符串 ``"false"``，
    而 ``bool("false") is True`` —— 故事就这么“结束”了。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "1"}:
            return True
        if text in {"false", "no", "0", ""}:
            return False
        return None
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def _normalize_threads(value, fallback: list[str] | None = None) -> list[str]:
    """未收束线索：去重保序，并同时受条数与 token 预算约束。

    合并交给导演做（只有它知道哪条已被收束），后端只负责别让列表无限膨胀。
    LLM 未给出该键时沿用上一场的列表，而不是当成"线索全部收束了"；
    继承进来的列表同样要过预算，否则超长内容会沿着谱系一直传下去。
    """
    items = value if isinstance(value, list) else (fallback or [])
    seen: set[str] = set()
    threads: list[str] = []
    used = 0
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        text = fit_lines([text], ContextBudget(max_tokens=_THREAD_ITEM_TOKENS)).text
        cost = estimate_tokens(text)
        if threads and used + cost > _THREADS_BUDGET_TOKENS:
            break
        threads.append(text)
        used += cost
        if len(threads) >= MAX_UNRESOLVED_THREADS:
            break
    return threads


_PLAN_PROMPT = """你是一位影视导演。请为剧情推演规划下一个场景。

【主线目标（用户设定，不可更改）】
{narrative_goal}

【本场意图】
{scene_intent}

【已完成场景历史（最近 5 场）】
{history}

【最近场次的结果与未收束线索】
{recent_results}

【可用角色】
{characters}

请挑选 2-6 名最合适的角色，设定场景。本场必须服务于主线目标；若本场意图与主线目标冲突，
以主线目标为准。优先推进尚未收束的线索。严格输出 JSON（不要额外文字）：
{{
  "name": "场景名",
  "description": "场景描述与期望走向（不强制结果）",
  "participating_characters": ["角色名1", "角色名2"],
  "location": "场景地点",
  "initial_conditions": {{"key": "value"}},
  "max_turns": 12,
  "opening_narration": "开场白/旁白，营造氛围"
}}
"""

_EVAL_PROMPT = """你是一位影视导演，正在评估刚刚模拟完的场景。

【主线目标（用户设定，不可更改）】
{narrative_goal}

【结局判定标准】
{ending_criteria}

【本场开始前的主线推进度】
{prior_progress}

【前情提要（本场之前的因果谱系，按时间顺序）】
{prior_synopses}

【截至上一场的未收束线索】
{prior_threads}

【本场预设】
{scene_brief}

【参演角色设定（导演视角，含角色本人并不知道的信息）】
{character_profiles}

【场景对白记录】
{transcript}

评分要求：
- 角色一致性请对照上面的角色设定；若某角色说出了它本不该知道的信息，应显著扣分；
- plot_deviation_score 请对照【主线目标】判断，越偏离分越高；
- story_progress 是 0-1 的主线推进度（对照主线目标估算已走到哪里），必须不小于上面给出的
  本场开始前的推进度；本场没有实质推进时给与之相同的值；
- unresolved_threads 请输出**更新后的完整列表**：保留仍未收束的旧线索、删去本场已收束的、
  追加本场新开的，最近提及的排在前面；
- 结局判定只看【结局判定标准】与【主线目标】，并对照【前情提要】确认条件是否真的已在
  前面的场次里完成（多条件的结局往往跨场次达成）；不得把本场演出来的任意告一段落
  当成故事结局。

请客观评估并严格输出 JSON（不要额外文字）：
{{
  "synopsis": "场景梗概（50-100字）",
  "narrative_goal_score": 0-10,
  "dramatic_tension_score": 0-10,
  "plot_deviation_score": 0-10,
  "character_consistency_score": 0-10,
  "story_progress": 0.0-1.0,
  "is_ending_reached": true|false,
  "ending_reason": "若已抵达结局，说明理由，否则空字符串",
  "unresolved_threads": ["未收束的线索1", "未收束的线索2"],
  "recommended_decision": "continue|next_scene|rollback",
  "rollback_reason": "若建议回滚，说明原因，否则空字符串"
}}
"""


class DirectorAgent:
    """导演智能体。"""

    def __init__(
        self,
        project_id: str,
        graph_manager: GraphManager | None = None,
        snapshot_manager=None,
        temperature: float | None = None,
    ):
        self.project_id = project_id
        self.graph = graph_manager
        self.snapshot_manager = snapshot_manager
        # 非空时统一覆盖三档温度；缺省则规划要创意、评估与决策要一致（工单04 D15）
        self.temperature = temperature
        self.model = settings.director_model

    def _temp(self, default: float) -> float:
        return self.temperature if self.temperature is not None else default

    # ---- 场景规划 ----
    async def plan_scene(
        self,
        branch_id: str,
        narrative_goal: str,
        available_characters: list[CharacterCard],
        history_scenes: list[Scene] | None = None,
        scene_intent: str = "",
        recent_results: list[tuple[Scene, SceneEvaluation]] | None = None,
    ) -> SceneConfig:
        char_desc = "\n".join(self._describe_for_plan(c) for c in available_characters)
        history_text = "（暂无历史场景）"
        if history_scenes:
            lines = []
            for s in history_scenes[-5:]:
                lines.append(f"- 【{s.name}】@{s.location}：{s.description[:60]}（已完成 {s.turns_completed} 轮）")
            history_text = "\n".join(lines)
        prompt = _PLAN_PROMPT.format(
            narrative_goal=narrative_goal or "（用户未设定主线目标，请依据历史场景自行把握大方向）",
            scene_intent=scene_intent or "（未指定，由你依据主线目标与未收束线索决定）",
            characters=char_desc,
            history=history_text,
            recent_results=self._describe_recent_results(recent_results or []),
        )
        raw = await chat_safe(
            [{"role": "user", "content": prompt}],
            temperature=self._temp(settings.DIRECTOR_PLAN_TEMPERATURE),
            model=self.model,
        )
        data, ok = _extract_json(raw)
        if not ok:
            logger.warning("导演规划返回的内容无法解析为 JSON，将全部走兜底：%s", raw[:300])

        chosen_ids, missed = self._match_characters(
            data.get("participating_characters", []) or [], available_characters
        )
        if missed:
            logger.warning("导演选中的角色名无法匹配，已忽略：%s", missed)
        if not chosen_ids:
            chosen_ids = self._fallback_characters(available_characters, history_scenes)
            logger.warning("导演未选出任何有效角色，按最近出场频次兜底为：%s", chosen_ids)

        return SceneConfig(
            name=data.get("name", "未命名场景"),
            description=data.get("description") or scene_intent or narrative_goal,
            participating_characters=chosen_ids,
            location=data.get("location", "未知地点"),
            initial_conditions=data.get("initial_conditions", {}) or {},
            max_turns=int(data.get("max_turns", 12) or 12),
            speaker_mode=settings.DEFAULT_SPEAKER_MODE,
            opening_narration=data.get("opening_narration", ""),
        )

    # ---- 场景评估 ----
    async def evaluate_scene(
        self,
        scene: Scene,
        dialogue_log: list[DialogueTurn],
        characters: list[CharacterCard] | None = None,
        narrative_goal: str = "",
        ending_criteria: str = "",
        prior_progress: float = PROGRESS_UNAVAILABLE,
        prior_threads: list[str] | None = None,
        prior_synopses: list[str] | None = None,
    ) -> SceneEvaluation:
        transcript = await self._build_transcript(dialogue_log)
        # 钳制基线：不可用（无历史评估）时按 0 起算，但仍要区分于"历史进度确实是 0"
        baseline = prior_progress if prior_progress >= 0 else 0.0
        revision = goal_revision(narrative_goal)
        # 继承进来的线索也要过预算：库里可能存着本次预算之前写入的超长列表
        prior = _normalize_threads(None, fallback=prior_threads)
        prompt = _EVAL_PROMPT.format(
            narrative_goal=narrative_goal or "（用户未设定主线目标，plot_deviation_score 请保守给出）",
            ending_criteria=ending_criteria or "（用户未给出明确的结局标准）",
            prior_progress=f"{baseline:.2f}"
            if prior_progress >= 0
            else "（暂无历史评估，本场是主线的起点）",
            prior_synopses=self._fit_synopses(prior_synopses or []),
            prior_threads="\n".join(f"- {t}" for t in prior) or "（暂无）",
            scene_brief=self._scene_brief(scene),
            character_profiles=self._describe_for_eval(characters or []),
            transcript=transcript,
        )
        raw = await chat_safe(
            [{"role": "user", "content": prompt}],
            temperature=self._temp(settings.DIRECTOR_EVAL_TEMPERATURE),
            model=self.model,
        )
        data, ok = _extract_json(raw)
        if not ok:
            # 不能静默给一份"看起来正常"的中位分：置为不可用，后续决策与前端都据此跳过
            logger.warning("场景 %s 的评估结果无法解析为 JSON：%s", scene.scene_id, raw[:300])
            result = unavailable_evaluation(scene.scene_id)
            result.synopsis = "（评估结果解析失败，分数不可信）"
            result.unresolved_threads = _normalize_threads(None, fallback=prior)
            result.goal_revision = revision
            return result

        def _score(key: str) -> float:
            parsed = _parse_number(data.get(key, 5))
            return 5.0 if parsed is None else max(0.0, min(10.0, parsed))

        rec = data.get("recommended_decision", DecisionType.NEXT_SCENE.value)
        if rec not in {d.value for d in DecisionType}:
            rec = DecisionType.NEXT_SCENE.value

        rollback_suggestion = None
        if rec == DecisionType.ROLLBACK.value:
            rollback_suggestion = {"reason": data.get("rollback_reason", "")}

        raw_progress = _parse_progress(data.get("story_progress"))
        if raw_progress < 0:
            # 缺失/非法不能当成"进度 0"：那会伪造一次停滞信号，也会让进度条掉回去
            logger.warning("场景 %s 的评估未给出可用的 story_progress", scene.scene_id)
            progress, stalled = PROGRESS_UNAVAILABLE, False
        else:
            progress = max(raw_progress, baseline)
            stalled = raw_progress <= baseline

        ending_reached, ending_reason = self._resolve_ending(
            data, scene.scene_id, narrative_goal, ending_criteria
        )

        return SceneEvaluation(
            scene_id=scene.scene_id,
            synopsis=data.get("synopsis", ""),
            narrative_goal_score=_score("narrative_goal_score"),
            dramatic_tension_score=_score("dramatic_tension_score"),
            plot_deviation_score=_score("plot_deviation_score"),
            character_consistency_score=_score("character_consistency_score"),
            recommended_decision=rec,
            rollback_suggestion=rollback_suggestion,
            story_progress=progress,
            story_progress_raw=raw_progress,
            progress_stalled=stalled,
            goal_revision=revision,
            is_ending_reached=ending_reached,
            ending_reason=ending_reason,
            unresolved_threads=_normalize_threads(
                data.get("unresolved_threads"), fallback=prior
            ),
        )

    @staticmethod
    def _fit_synopses(synopses: list[str]) -> str:
        """把因果谱系上的梯概装进预算。尾部（最近几场）优先保留。"""
        if not synopses:
            return "（本场之前没有已评估的场次）"
        return fit_lines(
            synopses, ContextBudget(max_tokens=_HISTORY_BUDGET_TOKENS, strategy=TAIL_ONLY)
        ).text

    @staticmethod
    def _resolve_ending(
        data: dict, scene_id: str, narrative_goal: str, ending_criteria: str
    ) -> tuple[bool, str]:
        """结局判定。没有任何用户锚点时一律判否。

        既无主线目标又无结局标准时，导演唯一能对照的就是它自己刚演出来的内容 ——
        那正是自评系统宣布"故事讲完了"的典型失效模式。
        """
        reached = _parse_bool(data.get("is_ending_reached", False))
        if reached is None:
            logger.warning(
                "场景 %s 的 is_ending_reached 无法判定（%r），按未抵达结局处理",
                scene_id,
                data.get("is_ending_reached"),
            )
            reached = False
        if reached and not (narrative_goal or ending_criteria):
            logger.warning(
                "场景 %s 的评估声称已抵达结局，但项目没有主线目标/结局标准，已忽略", scene_id
            )
            return False, ""
        return reached, str(data.get("ending_reason", "") or "") if reached else ""

    # ---- 决策 ----
    async def make_decision(
        self,
        evaluation: SceneEvaluation,
        human_override: DirectorDecision | None = None,
    ) -> DirectorDecision:
        """综合评估与人类干预做出决策。human_override 优先。"""
        if human_override is not None:
            return human_override

        # 评分不可用时绝不能让它进阈值规则：-1 会把每一条下限判断都误触发，
        # 而 rollback 在 apply_decision 里是真的会建分支、真的改剧情状态。
        if is_evaluation_unavailable(evaluation):
            logger.warning(
                "场景 %s 的评估不可用，跳过评分规则，按保守默认决策 %s",
                evaluation.scene_id,
                evaluation.recommended_decision,
            )
            return DirectorDecision(decision_type=evaluation.recommended_decision)

        # 基于评分的规则化推荐（与 CLAUDE.md 5.3 评分维度一致）
        decision_type = evaluation.recommended_decision
        if (
            evaluation.narrative_goal_score < 4
            or evaluation.character_consistency_score < 5
        ):
            decision_type = DecisionType.ROLLBACK.value
        elif evaluation.dramatic_tension_score < 3:
            decision_type = DecisionType.CONTINUE.value

        decision = DirectorDecision(decision_type=decision_type)
        if decision_type == DecisionType.CONTINUE.value:
            decision.extra_turns = 6
        elif decision_type == DecisionType.ROLLBACK.value:
            decision.rollback_notes = (
                evaluation.rollback_suggestion or {}
            ).get("reason", "评分过低，建议回滚重演")
        return decision

    # ---- 全局查询（导演专属权限）----
    async def query_character_state(
        self, character_id: str, scene_id: str = ""
    ) -> CharacterState:
        """读取角色在指定场景（缺省：最近快照）时点的内部状态。

        与 Inspection 面板、总结智能体共用同一读取路径（工单17）。
        """
        state, _ = await inspection.load_character_state(
            self.project_id, character_id, scene_id=scene_id
        )
        return state

    async def query_graph(self, cypher: str) -> list[dict]:
        if self.graph is None:
            return []
        return await self.graph.query(cypher)

    # ---- 辅助 ----
    @staticmethod
    def _describe_for_plan(c: CharacterCard) -> str:
        facts = "；".join(c.known_facts[:3]) or "无"
        rel = (
            "；".join(
                f"对 {s.target_character_id}：{s.relation_type}"
                for s in list(c.relationships.values())[:3]
            )
            or "无"
        )
        return (
            f"- {c.name}：{(c.persona or '（待补充）')[:150]}\n"
            f"  当前：{c.current_emotion} @ {c.current_location or '未知'}"
            f"｜目标：{c.current_goal or '顺其自然'}\n"
            f"  已知：{facts}\n"
            f"  关系：{rel}"
        )

    @staticmethod
    def _describe_recent_results(results: list[tuple[Scene, SceneEvaluation]]) -> str:
        """规划用的"上一场结果"块。

        场景历史给的是**演之前的预设**，这里给的才是**演出来的结果**：导演自己写的
        梗概与仍未收束的线索。没有它，导演规划下一场时看不到上一场究竟发生了什么。
        """
        if not results:
            return "（暂无已评估的场次）"
        blocks = []
        for scene, ev in results:
            synopsis = ev.synopsis or "（无梗概）"
            blocks.append(f"- 【{scene.name}】{synopsis}")
        latest = results[-1][1]
        threads = _normalize_threads(None, fallback=latest.unresolved_threads)
        if threads:
            blocks.append("未收束线索：")
            blocks.extend(f"  · {t}" for t in threads)
        return "\n".join(blocks)

    @staticmethod
    def _describe_for_eval(characters: list[CharacterCard]) -> str:
        """评估用的角色设定块。

        这里注入 unknown_facts 是契约1 的合法例外：导演拥有全知视角，且本文本只进
        导演 prompt，不进入任何角色可见上下文。没有它，角色一致性评分只能靠猜。
        """
        if not characters:
            return "（未提供角色设定，角色一致性评分请保守给出）"
        blocks = []
        for c in characters:
            known = "；".join(c.known_facts[:5]) or "无"
            unknown = "；".join(c.unknown_facts[:5]) or "无"
            blocks.append(
                f"- {c.name}：{(c.persona or '（待补充）')[:200]}\n"
                f"  说话风格：{c.speech_style or '自然'}\n"
                f"  已知：{known}\n"
                f"  **不知道**：{unknown}"
            )
        return "\n".join(blocks)

    @staticmethod
    def _scene_brief(scene: Scene) -> str:
        lines = [f"{scene.name} @ {scene.location}".strip(" @"), scene.description]
        narration = scene.initial_conditions.get("opening_narration")
        if narration:
            lines.append(f"开场：{narration}")
        conditions = {
            k: v for k, v in scene.initial_conditions.items() if k != "opening_narration"
        }
        if conditions:
            lines.append("初始条件：" + "；".join(f"{k}={v}" for k, v in conditions.items()))
        return "\n".join(line for line in lines if line)

    @staticmethod
    def _match_characters(
        names: list, cards: list[CharacterCard]
    ) -> tuple[list[str], list[str]]:
        """三级匹配角色名 → ID：精确 → 去空格 → 子串包含。返回 (命中 id, 未命中名)。

        LLM 常返回别名或带头衔的写法（"张丞相"），旧实现只做精确匹配，落空后直接
        取角色列表前两个（即实体抽取顺序），且不留日志。
        """
        by_exact = {c.name: c for c in cards if c.name}
        by_squashed = {c.name.replace(" ", ""): c for c in cards if c.name}
        # 最长名优先，避免"王"抢在"王子"前面命中
        by_length = sorted((c for c in cards if c.name), key=lambda c: len(c.name), reverse=True)

        hit: list[str] = []
        missed: list[str] = []
        for raw in names:
            name = str(raw).strip()
            if not name:
                continue
            card = by_exact.get(name) or by_squashed.get(name.replace(" ", ""))
            if card is None:
                card = next(
                    (c for c in by_length if c.name in name or name in c.name), None
                )
            if card is None:
                missed.append(name)
            elif card.character_id not in hit:
                hit.append(card.character_id)
        return hit, missed

    @staticmethod
    def _fallback_characters(
        cards: list[CharacterCard], history_scenes: list[Scene] | None, limit: int = 3
    ) -> list[str]:
        """选角全部落空时的兜底：按最近出场频次取人，而非实体抽取顺序的前两个。"""
        valid = {c.character_id for c in cards}
        counter: Counter[str] = Counter()
        for s in (history_scenes or [])[-5:]:
            counter.update(cid for cid in s.participating_characters if cid in valid)
        ranked = [cid for cid, _ in counter.most_common()]
        ranked += [c.character_id for c in cards if c.character_id not in ranked]
        return ranked[: max(2, min(limit, len(ranked)))]

    async def _build_transcript(self, log: list[DialogueTurn]) -> str:
        """把对白装进导演的上下文预算（工单27）。

        预算默认足以容纳整场，只有 continue 续跑累积出超长场景才会真的压缩；
        无论哪种策略都不得截掉结尾——评估最需要的就是结局。
        """
        lines = self._transcript_lines(log)
        if not lines:
            return "（无对话）"
        fitted = await compact_lines(
            lines,
            ContextBudget(
                max_tokens=settings.DIRECTOR_TRANSCRIPT_BUDGET,
                strategy=settings.DIRECTOR_TRANSCRIPT_STRATEGY,
            ),
            model=self.model,
        )
        if fitted.compacted:
            logger.info("导演评估上下文已压缩，省略 %d 轮", fitted.dropped)
        return fitted.text

    @staticmethod
    def _transcript_lines(log: list[DialogueTurn]) -> list[str]:
        """导演有全知权，故保留 inner_thought。"""
        lines = []
        for t in log:
            parts = []
            if t.action:
                parts.append(f"*{t.action}*")
            if t.dialogue:
                parts.append(t.dialogue)
            if t.inner_thought:
                parts.append(f"[{t.inner_thought}]")
            lines.append(f"{t.character_name}: {' '.join(parts)}")
        return lines
