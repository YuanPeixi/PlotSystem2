"""世界规则条目提取：从种子文本中提取 LoreEntry。"""

from __future__ import annotations

import json
import re

from backend.models import LoreEntry
from backend.utils.llm import chat_safe
from backend.utils.logger import get_logger

logger = get_logger("graphrag.world_rules")

_LORE_PROMPT = """你是世界观设定专家。从下面的文本中提取"世界规则/设定条目"。
每条应是独立的设定知识（如世界观背景、魔法体系、势力关系、风俗规则等）。

严格输出 JSON 数组（不要额外文字）：
[
  {{"content": "设定内容", "keywords": ["触发关键词"], "scope": "global", "priority": 5}}
]

文本：
\"\"\"
{text}
\"\"\"
"""


def _extract_json_array(raw: str) -> list:
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    else:
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            raw = m.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


class WorldRulesExtractor:
    """世界观条目提取器。"""

    async def extract(self, texts: list[str]) -> list[LoreEntry]:
        entries: list[LoreEntry] = []
        for text in texts:
            prompt = _LORE_PROMPT.format(text=text[:6000])
            try:
                raw = await chat_safe([{"role": "user", "content": prompt}], temperature=0.3)
            except Exception as exc:  # noqa: BLE001
                # 单段世界规则提取失败不应中断整个构建，跳过该段。
                logger.warning("世界规则提取失败，已跳过一段：%s", exc)
                continue
            for item in _extract_json_array(raw):
                content = (item.get("content") or "").strip()
                if not content:
                    continue
                entries.append(
                    LoreEntry(
                        content=content,
                        keywords=list(item.get("keywords", []) or []),
                        scope=item.get("scope", "global"),
                        priority=int(item.get("priority", 5) or 5),
                    )
                )
        return entries
