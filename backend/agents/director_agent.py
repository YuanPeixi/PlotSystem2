"""DirectorAgent：导演智能体。

唯一拥有全局知识图谱访问权、可查看所有角色内部状态的实体。
负责场景规划、模拟后评估、决策（继续/下一场/回滚）。
temperature 默认 0.3（追求一致性）。
"""

from __future__ import annotations

import json
import re

from backend.knowledge_graph import GraphManager
from backend.models import (
    CharacterCard,
    CharacterState,
    DecisionType,
    DialogueTurn,
    DirectorDecision,
    Scene,
    SceneConfig,
    SceneEvaluation,
)
from backend.config import settings
from backend.utils.llm import chat_safe
from backend.utils.logger import get_logger

logger = get_logger("agents.director")


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
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


_PLAN_PROMPT = """你是一位影视导演。请为剧情推演规划下一个场景。

【叙事目标】
{goal}

【可用角色】
{characters}

请挑选 2-6 名最合适的角色，设定场景。严格输出 JSON（不要额外文字）：
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

【场景预设目标】
{description}

【场景对白记录】
{transcript}

请客观评估并严格输出 JSON（不要额外文字）：
{{
  "synopsis": "场景梗概（50-100字）",
  "narrative_goal_score": 0-10,
  "dramatic_tension_score": 0-10,
  "plot_deviation_score": 0-10,
  "character_consistency_score": 0-10,
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
        temperature: float = 0.3,
    ):
        self.project_id = project_id
        self.graph = graph_manager
        self.snapshot_manager = snapshot_manager
        self.temperature = temperature
        self.model = settings.director_model

    # ---- 场景规划 ----
    async def plan_scene(
        self,
        branch_id: str,
        narrative_goal: str,
        available_characters: list[CharacterCard],
    ) -> SceneConfig:
        char_desc = "\n".join(
            f"- {c.name}：{(c.persona or '')[:80]}（目标：{c.current_goal}）"
            for c in available_characters
        )
        prompt = _PLAN_PROMPT.format(goal=narrative_goal, characters=char_desc)
        raw = await chat_safe([{"role": "user", "content": prompt}], temperature=self.temperature, model=self.model)
        data = _extract_json(raw)

        name_to_id = {c.name: c.character_id for c in available_characters}
        chosen_names = data.get("participating_characters", []) or []
        chosen_ids = [name_to_id[n] for n in chosen_names if n in name_to_id]
        if not chosen_ids:  # 兜底：至少取前两个
            chosen_ids = [c.character_id for c in available_characters[:2]]

        return SceneConfig(
            name=data.get("name", "未命名场景"),
            description=data.get("description", narrative_goal),
            participating_characters=chosen_ids,
            location=data.get("location", "未知地点"),
            initial_conditions=data.get("initial_conditions", {}) or {},
            max_turns=int(data.get("max_turns", 12) or 12),
            opening_narration=data.get("opening_narration", ""),
        )

    # ---- 场景评估 ----
    async def evaluate_scene(
        self,
        scene: Scene,
        dialogue_log: list[DialogueTurn],
    ) -> SceneEvaluation:
        transcript = self._format_transcript(dialogue_log)
        prompt = _EVAL_PROMPT.format(description=scene.description, transcript=transcript)
        raw = await chat_safe([{"role": "user", "content": prompt}], temperature=self.temperature, model=self.model)
        data = _extract_json(raw)

        def _score(key: str) -> float:
            try:
                return max(0.0, min(10.0, float(data.get(key, 5))))
            except (TypeError, ValueError):
                return 5.0

        rec = data.get("recommended_decision", DecisionType.NEXT_SCENE.value)
        if rec not in {d.value for d in DecisionType}:
            rec = DecisionType.NEXT_SCENE.value

        rollback_suggestion = None
        if rec == DecisionType.ROLLBACK.value:
            rollback_suggestion = {"reason": data.get("rollback_reason", "")}

        return SceneEvaluation(
            scene_id=scene.scene_id,
            synopsis=data.get("synopsis", ""),
            narrative_goal_score=_score("narrative_goal_score"),
            dramatic_tension_score=_score("dramatic_tension_score"),
            plot_deviation_score=_score("plot_deviation_score"),
            character_consistency_score=_score("character_consistency_score"),
            recommended_decision=rec,
            rollback_suggestion=rollback_suggestion,
        )

    # ---- 决策 ----
    async def make_decision(
        self,
        evaluation: SceneEvaluation,
        human_override: DirectorDecision | None = None,
    ) -> DirectorDecision:
        """综合评估与人类干预做出决策。human_override 优先。"""
        if human_override is not None:
            return human_override

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
    async def query_character_state(self, character_id: str) -> CharacterState:
        return CharacterState(character_id=character_id)

    async def query_graph(self, cypher: str) -> list[dict]:
        if self.graph is None:
            return []
        return await self.graph.query(cypher)

    # ---- 辅助 ----
    @staticmethod
    def _format_transcript(log: list[DialogueTurn]) -> str:
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
        return "\n".join(lines) or "（无对话）"
