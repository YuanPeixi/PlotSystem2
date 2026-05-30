"""从图谱中的 Character 实体生成初始 CharacterCard。"""

from __future__ import annotations

import json
import re

from backend.models import CharacterCard, Entity
from backend.utils.llm import chat_safe
from backend.utils.logger import get_logger

logger = get_logger("graphrag.persona")

_PERSONA_PROMPT = """你是角色塑造专家。基于以下人物信息，生成结构化角色卡。

人物名：{name}
已知描述：{description}
上下文片段：
{context}

严格输出 JSON（不要额外文字）：
{{
  "persona": "性格、背景、动机的综合描述（100-200字）",
  "appearance": "外貌描述（可推测）",
  "speech_style": "说话习惯、口癖",
  "current_emotion": "初始情绪",
  "current_goal": "初始目标",
  "known_facts": ["该角色明确知道的事实"],
  "unknown_facts": ["该角色不知道但读者/导演知道的事实"]
}}
"""


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


class PersonaBuilder:
    """角色卡生成器。"""

    async def build(
        self,
        project_id: str,
        entity: Entity,
        context: str = "",
    ) -> CharacterCard:
        """为单个 Character 实体生成角色卡。"""
        prompt = _PERSONA_PROMPT.format(
            name=entity.name,
            description=entity.description or "（无）",
            context=context[:3000] or "（无额外上下文）",
        )
        raw = await chat_safe([{"role": "user", "content": prompt}], temperature=0.6)
        data = _extract_json(raw)

        return CharacterCard(
            character_id=entity.entity_id,
            project_id=project_id,
            name=entity.name,
            persona=data.get("persona", entity.description),
            appearance=data.get("appearance", ""),
            speech_style=data.get("speech_style", ""),
            known_facts=list(data.get("known_facts", []) or []),
            unknown_facts=list(data.get("unknown_facts", []) or []),
            current_emotion=data.get("current_emotion", "平静"),
            current_goal=data.get("current_goal", ""),
        )

    async def build_many(
        self,
        project_id: str,
        entities: list[Entity],
        context: str = "",
    ) -> list[CharacterCard]:
        cards: list[CharacterCard] = []
        for e in entities:
            if e.entity_type != "Character":
                continue
            cards.append(await self.build(project_id, e, context))
        return cards
