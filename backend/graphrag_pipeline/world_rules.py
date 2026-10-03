"""世界规则条目提取：从种子文本中提取 LoreEntry，并判定每条设定该让谁知道。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace

from backend.models import CharacterCard, LoreEntry
from backend.utils.llm import chat_safe
from backend.utils.logger import get_logger

logger = get_logger("graphrag.world_rules")

_LORE_PROMPT = """你是世界观设定专家。从下面的文本中提取"世界规则/设定条目"。
每条应是独立的设定知识（如世界观背景、魔法体系、势力关系、风俗规则等）。

严格输出 JSON 数组（不要额外文字）：
[
  {{"content": "设定内容", "keywords": ["触发关键词"], "priority": 5}}
]

文本：
\"\"\"
{text}
\"\"\"
"""

_VISIBILITY_PROMPT = """你是剧情设定审校。下面这些世界设定会写进角色扮演的提示词，
分错了，角色就会知道它本不该知道的秘密。请判断每条设定应当让谁知道。

可见性只有三种：
- public：所有角色都能感知的公开事实或常识（公开举行的仪式、众所周知的传说、地理与风俗）；
- private：只有部分角色知道。known_by 列出知情角色的名字，必须与下方角色名完全一致；
- hidden：没有任何角色完整知道（只有作者/导演掌握的真相，或对整体信息差的叙述）。

判断规则：
1. known_by 只能列出对这条设定**全部内容**都知情的角色；
   只要一条设定涉及某个角色"不知道的事实"，就绝不能让这个角色知道它；
2. 由此推出：涉及任何一个角色"不知道的事实"的设定，都不能是 public；
3. 拿不准时选 private 或 hidden，不要选 public。

角色：
{characters}

种子文本（节选）：
\"\"\"
{text}
\"\"\"

设定条目：
{entries}

严格输出 JSON 数组（不要额外文字），每个条目一项：
[
  {{"index": 0, "visibility": "public", "known_by": []}},
  {{"index": 1, "visibility": "private", "known_by": ["角色名"]}}
]
"""

PUBLIC = "public"
PRIVATE = "private"
HIDDEN = "hidden"
#: 分类没成功（调用失败、漏判、取值非法、知情者对不上任何角色）。与 hidden 同样不发给任何角色。
WITHHELD = "withheld"
#: 判定调用本身失败时的 note。与"判了但判不出"区分开：前者重试可能就好了。
CALL_FAILED = "判定调用失败"

_BATCH_SIZE = 20
_MAX_FACTS = 15
_FACT_CHARS = 120


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
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


class WorldRulesExtractor:
    """世界观条目提取器。只抽内容，可见性交给 `LoreVisibilityClassifier`。"""

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
                if not isinstance(item, dict):
                    continue
                content = (item.get("content") or "").strip()
                if not content:
                    continue
                entries.append(
                    LoreEntry(
                        content=content,
                        keywords=list(item.get("keywords", []) or []),
                        # 抽取时不知道有哪些角色，判不了可见性；先记 global，
                        # 真正的 scope 由分类器给出，未经分类的条目不得直接分发（工单29）
                        scope="global",
                        priority=int(item.get("priority", 5) or 5),
                    )
                )
        return entries


@dataclass
class LoreVerdict:
    """一条设定的可见性判定。"""

    entry: LoreEntry
    visibility: str = WITHHELD
    #: private 时的知情角色 id
    holders: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def distributed(self) -> bool:
        return self.visibility == PUBLIC or (self.visibility == PRIVATE and bool(self.holders))


def lore_for_character(verdicts: list[LoreVerdict], character_id: str) -> list[LoreEntry]:
    """按判定结果取出发给某个角色的设定。

    多人知情的私有设定按知情者各发一份 `character:{id}` 副本：卡片本来就是每人一份拷贝，
    不需要给 LoreEntry 加"知情者列表"字段。
    """
    own_scope = f"character:{character_id}"
    result: list[LoreEntry] = []
    for v in verdicts:
        if v.visibility == PUBLIC:
            result.append(replace(v.entry, scope="global"))
        elif v.visibility == PRIVATE and character_id in v.holders:
            result.append(replace(v.entry, scope=own_scope))
    return result


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _describe_characters(cards: list[CharacterCard]) -> str:
    blocks = []
    for card in cards:
        known = "；".join(_clip(f, _FACT_CHARS) for f in card.known_facts[:_MAX_FACTS]) or "（无）"
        unknown = "；".join(_clip(f, _FACT_CHARS) for f in card.unknown_facts[:_MAX_FACTS]) or "（无）"
        blocks.append(f"【{card.name}】\n  知道：{known}\n  不知道：{unknown}")
    return "\n".join(blocks) or "（无角色）"


def _unknown_facts_truncated(cards: list[CharacterCard]) -> list[str]:
    """哪些角色的 unknown_facts 被 `_describe_characters` 裁掉了内容（条数或单条长度）。

    分类器只能依据它看到的证据判断。某个角色"不知道"某件事，靠的正是这条 unknown_fact
    出现在它的证据里——裁掉了，分类器就没有反证，可能据此真的判为 public，把设定发给
    这个本不该知道的角色（工单29 评审）。只查 unknown_facts：known_facts 被裁只影响
    private 的知情者名单是否齐全，漏发比泄密代价小得多，不必连带收紧（§4 的不对称前提）。
    数值上限本身不是修复——任何固定上限都可能被超过，这里是结构性的兜底，不是靠调大
    `_MAX_FACTS`/`_FACT_CHARS` 就能顶替的（同 WorldState 预算"两道闸门缺一不可"的教训）。
    """
    truncated = []
    for card in cards:
        over_count = len(card.unknown_facts) > _MAX_FACTS
        over_length = any(
            len(" ".join(str(f).split())) > _FACT_CHARS for f in card.unknown_facts[:_MAX_FACTS]
        )
        if over_count or over_length:
            truncated.append(card.name)
    return truncated


def _is_index(value: object) -> bool:
    # json 里的 true 会被当成 1，必须先排除 bool
    return isinstance(value, int) and not isinstance(value, bool)


class LoreVisibilityClassifier:
    """判定每条设定的可见性（工单29）。构建与已有项目的迁移共用这一份判定。

    **失败即收紧**：任何拿不准的情况都不发给角色，绝不退回 global。漏掉一条公开设定，
    角色只是少知道一点背景；泄露一条秘密，这场戏的信息差就没了，而且无痕（契约1）。

    prompt 里有全体角色的 unknown_facts —— 这是构建期、面向导演的判定，与
    PersonaBuilder 同级，结果只决定 scope，原文不进任何角色可见的上下文。
    """

    async def classify(
        self, entries: list[LoreEntry], cards: list[CharacterCard], seed_text: str = ""
    ) -> list[LoreVerdict]:
        if not entries:
            return []
        if not cards:
            # 没有角色就没有"谁知道"可言，也就无从分发
            return [LoreVerdict(e, note="项目没有角色") for e in entries]

        incomplete = _unknown_facts_truncated(cards)
        if incomplete:
            logger.warning(
                "角色 %s 的 unknown_facts 被裁剪（超过 %d 条或单条超过 %d 字），"
                "本次判定缺少「谁不知道」的完整证据，public 结论一律收紧为不发",
                "、".join(incomplete),
                _MAX_FACTS,
                _FACT_CHARS,
            )

        verdicts: list[LoreVerdict] = []
        for start in range(0, len(entries), _BATCH_SIZE):
            batch = entries[start : start + _BATCH_SIZE]
            verdicts.extend(
                await self._classify_batch(batch, cards, seed_text, bool(incomplete))
            )

        withheld = [v for v in verdicts if v.visibility == WITHHELD]
        if withheld:
            logger.warning(
                "%d 条世界设定未能判定可见性，已不发给任何角色：%s",
                len(withheld),
                "；".join(f"{_clip(v.entry.content, 30)}（{v.note}）" for v in withheld[:5]),
            )
        return verdicts

    async def _classify_batch(
        self,
        batch: list[LoreEntry],
        cards: list[CharacterCard],
        seed_text: str,
        incomplete_evidence: bool,
    ) -> list[LoreVerdict]:
        prompt = _VISIBILITY_PROMPT.format(
            characters=_describe_characters(cards),
            text=seed_text[:6000] or "（无）",
            entries="\n".join(
                f"[{i}] {_clip(e.content, 400)}（关键词：{'、'.join(map(str, e.keywords)) or '无'}）"
                for i, e in enumerate(batch)
            ),
        )
        try:
            raw = await chat_safe([{"role": "user", "content": prompt}], temperature=0.2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("世界设定可见性判定失败，本批 %d 条不发给任何角色：%s", len(batch), exc)
            return [LoreVerdict(e, note=CALL_FAILED) for e in batch]

        counts: dict[int, int] = {}
        first_item: dict[int, dict] = {}
        for item in _extract_json_array(raw):
            if isinstance(item, dict) and _is_index(item.get("index")):
                idx = item["index"]
                counts[idx] = counts.get(idx, 0) + 1
                first_item.setdefault(idx, item)

        name_to_id = {card.name.strip(): card.character_id for card in cards}
        verdicts: list[LoreVerdict] = []
        for i, e in enumerate(batch):
            if counts.get(i, 0) > 1:
                # 同一个 index 出现两次，就说明这份判定本身不可信——不管两次给出的
                # 可见性是否碰巧一致：模型没有遵守"一个 index 一项"的格式约束，
                # 挑其中一个采信（旧实现用 setdefault 悄悄留下第一项）等于在信任
                # 一份已经自相矛盾的回答。与 _verdict 里"知情者部分对不上就整条
                # 不发"同一条原则：任何不确定都按不发处理，不按"挑一个更安全的"处理。
                logger.warning(
                    "设定「%s」在同一批判定里 index=%d 重复出现 %d 次，判定不可靠，不发给任何角色",
                    _clip(e.content, 30),
                    i,
                    counts[i],
                )
                verdicts.append(LoreVerdict(e, note="重复索引，判定冲突"))
                continue
            verdict = self._verdict(e, first_item.get(i), name_to_id)
            if incomplete_evidence and verdict.visibility == PUBLIC:
                # 证据本身不全，"没找到不知道的理由"不能当成"所以是公开的"——
                # 那条反证可能就在被裁掉的那一截 unknown_facts 里（见 classify() 的判定）。
                verdict = LoreVerdict(e, note="unknown_facts 证据不完整，public 判定不可信")
            verdicts.append(verdict)
        return verdicts

    @staticmethod
    def _verdict(entry: LoreEntry, item: dict | None, name_to_id: dict[str, str]) -> LoreVerdict:
        if item is None:
            return LoreVerdict(entry, note="未返回判定")
        visibility = item.get("visibility")
        if visibility == PUBLIC:
            return LoreVerdict(entry, PUBLIC)
        if visibility == HIDDEN:
            return LoreVerdict(entry, HIDDEN)
        if visibility != PRIVATE:
            return LoreVerdict(entry, note=f"可见性取值非法：{visibility!r}")

        names = item.get("known_by")
        names = [str(n).strip() for n in names] if isinstance(names, list) else []
        unmatched = [n for n in names if n not in name_to_id]
        if unmatched:
            # 任一名字对不上都不发给任何人，哪怕其余名字都匹配上了：一个解析不出的
            # 知情者，本身就是"这份判定信不过"的信号，不能只采信凑巧匹配上的那部分
            # （工单29 §3.2 把它与调用失败、取值非法列为同一类失败，不是"部分成功"）。
            logger.warning(
                "设定「%s」的知情者对不上任何角色，整条不发给任何角色：%s",
                _clip(entry.content, 30),
                unmatched,
            )
            return LoreVerdict(entry, note="知情者对不上任何角色")
        holders = list(dict.fromkeys(name_to_id[n] for n in names))
        if not holders:
            return LoreVerdict(entry, note="private 未给出知情者")
        return LoreVerdict(entry, PRIVATE, holders)
