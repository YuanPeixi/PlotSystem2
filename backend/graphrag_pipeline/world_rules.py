"""世界规则条目提取：从种子文本中提取 LoreEntry，并判定每条设定该让谁知道。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace

from backend.models import (
    OBJECT_VISIBILITY_CHARACTER_PREFIX,
    OBJECT_VISIBILITY_GLOBAL,
    OBJECT_VISIBILITY_HIDDEN,
    CharacterCard,
    LoreEntry,
    WorldObject,
)
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
#: 送进分类 prompt 的设定正文上限。它只是把单次调用的最坏情况兜住——不是裁剪安全，
#: 真正安全的是下面"裁了就不敢信 public/private"这道闸门。调大这个数不会让裁剪消失，
#: 只会让触发閾值更难碰到。
_ENTRY_CONTENT_CHARS = 2000


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
    出现在它的证据里——裁掉了，分类器就可能在两个方向上都判错（评审第二轮）：
    把本该私有的设定判成 public（没找到任何人"不知道"的反证），或者把这个本不该
    知情的角色错判进某条 private 的 known_by（恰好是它自己那条"不知道"的反证被
    裁掉，分类器没有依据排除它）。因此只要检测到裁剪，public 与 private 都要收紧，
    不只是 public——hidden 不发给任何人，结构上已经最安全，不受影响。只查
    unknown_facts：known_facts 被裁只会让 private 的知情者名单**偏少**（遗漏一个
    真正的知情者），方向本身安全，不必连带收紧。
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
                "本次判定缺少「谁不知道」的完整证据，public/private 结论一律收紧为不发",
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
        # 分类器只能对它看到的正文下判断。裁给它的那一截若恰好是"看起来公开"的
        # 前半段，而秘密藏在后半段，分类器会在毫不知情的情况下给出 public——
        # 记下哪些条目被裁了，连同 incomplete_evidence 一起收紧，绝不能"分类器
        # 看前 N 字、分发时却把完整正文发出去"（评审复现：空种子文本 + 超长条目）。
        content_truncated: set[int] = set()
        lines = []
        for i, e in enumerate(batch):
            normalized = " ".join(str(e.content).split())
            if len(normalized) > _ENTRY_CONTENT_CHARS:
                content_truncated.add(i)
            lines.append(
                f"[{i}] {_clip(e.content, _ENTRY_CONTENT_CHARS)}"
                f"（关键词：{'、'.join(map(str, e.keywords)) or '无'}）"
            )
        prompt = _VISIBILITY_PROMPT.format(
            characters=_describe_characters(cards),
            text=seed_text[:6000] or "（无）",
            entries="\n".join(lines),
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
            if verdict.visibility in (PUBLIC, PRIVATE):
                # private 同样要收紧：知情者名单是"谁知道"，而"谁不知道"的反证恰恰
                # 来自各角色的 unknown_facts——若那条反证被裁掉，分类器可能把一个
                # 本不该知情的角色错判进 known_by，private 照样会把秘密发给他，
                # 不是只有 public 才有这个风险。hidden 不受影响：它不发给任何人，
                # 结构上已经是最安全的结论。
                if i in content_truncated:
                    verdict = LoreVerdict(e, note="设定正文被裁剪，判定不可信")
                elif incomplete_evidence:
                    verdict = LoreVerdict(e, note="unknown_facts 证据不完整，判定不可信")
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


# ---------------------------------------------------------------------------
# 物件（工单24）
# ---------------------------------------------------------------------------

_OBJECT_PROMPT = """你是剧情道具设定专家。从下面的文本中找出"角色可以对它做动作、且它会按某种规则作出反应"的物件：
法器、机关、带特殊功能的道具、暗格或密门等。普通陈设、武器的寻常用法、抽象概念都不算。

每个物件分两部分写，**不要混写**：
- public_description：在场的人用眼睛就能看到的部分（外观、摆放位置），一两句。
  不写它的功能、来历与触发条件；
- hidden_rules：触发条件、机关的开法、真实功能、限制，每条一句。

aliases 写文中对它的其他叫法：至少两个字，不要写角色名。

严格输出 JSON 数组（不要额外文字）；没有符合条件的物件就输出 []：
[
  {{"name": "物件名", "aliases": ["别名"], "public_description": "外观", "hidden_rules": ["规则"]}}
]

文本：
\"\"\"
{text}
\"\"\"
"""


@dataclass
class ObjectExtraction:
    """物件抽取结果。`failed_chunks` 非零时迁移脚本整个不写（让人重试）；构建照常落盘已抽到的。"""

    objects: list[WorldObject] = field(default_factory=list)
    failed_chunks: int = 0


def _str_list(value: object) -> list[str]:
    return [str(v) for v in value if isinstance(v, (str, int, float))] if isinstance(value, list) else []


class ObjectExtractor:
    """从种子文本抽取物件（工单24）。只抽内容，可见性交给 `classify_object_visibility`。"""

    async def extract(self, texts: list[str], character_names: set[str]) -> ObjectExtraction:
        result = ObjectExtraction()
        by_name: dict[str, WorldObject] = {}
        for text in texts:
            prompt = _OBJECT_PROMPT.format(text=text[:6000])
            try:
                raw = await chat_safe([{"role": "user", "content": prompt}], temperature=0.3)
            except Exception as exc:  # noqa: BLE001
                logger.warning("物件抽取失败，已跳过一段：%s", exc)
                result.failed_chunks += 1
                continue
            for item in _extract_json_array(raw):
                if not isinstance(item, dict):
                    continue
                name = " ".join(str(item.get("name") or "").split())
                if not name:
                    continue
                if name in character_names:
                    # 与角色同名的物件会让预过滤每轮命中；多半是模型把人当成了物件
                    logger.warning("抽取出的物件「%s」与角色同名，已丢弃", name)
                    continue
                aliases = _str_list(item.get("aliases"))
                rules = _str_list(item.get("hidden_rules"))
                existing = by_name.get(name)
                if existing is not None:
                    # 分块重叠或多段文本提到同一件东西：合并别名与规则，公开描述留第一份
                    existing.aliases = list(dict.fromkeys([*existing.aliases, *aliases]))
                    existing.hidden_rules = list(dict.fromkeys([*existing.hidden_rules, *rules]))
                    continue
                by_name[name] = WorldObject(
                    name=name,
                    aliases=aliases,
                    public_description=str(item.get("public_description") or "").strip(),
                    hidden_rules=rules,
                )
        result.objects = list(by_name.values())
        return result


def object_visibility(verdict: LoreVerdict) -> str:
    """把设定可见性判定映射成物件的 visibility，拿不准的一律 hidden（失败即收紧）。

    多人知情的私有物件暂时收紧为 hidden：`visibility` 只能指向一个角色，而设定那边按知情者
    各发一份副本的办法对项目级的单个物件文件不适用。PR-1 没有任何运行时读取 visibility，
    收紧没有代价；PR-2 把公开描述注入角色视野时再决定是否改成列表。
    """
    if verdict.visibility == PUBLIC:
        return OBJECT_VISIBILITY_GLOBAL
    if verdict.visibility == PRIVATE and len(verdict.holders) == 1:
        return f"{OBJECT_VISIBILITY_CHARACTER_PREFIX}{verdict.holders[0]}"
    if verdict.visibility == PRIVATE:
        logger.warning(
            "物件「%s」有 %d 位知情者，单值 visibility 表达不了，暂按 hidden 处理",
            _clip(verdict.entry.content, 20),
            len(verdict.holders),
        )
    return OBJECT_VISIBILITY_HIDDEN


async def classify_object_visibility(
    objects: list[WorldObject],
    cards: list[CharacterCard],
    seed_text: str,
    classifier: LoreVisibilityClassifier,
) -> list[LoreVerdict]:
    """判定每个物件"谁知道它的存在与外观"，就地写入 `visibility`，返回判定（供计数）。

    复用设定的分类器（工单29 的三道收紧安全网随之继承），送进去的只有名称与公开描述：
    隐藏规则按定义谁都不发，拿去分类只会让模型因为规则里的秘密把整个物件判成 hidden。
    """
    entries = [
        LoreEntry(
            lore_id=obj.object_id,
            content=f"{obj.name}：{obj.public_description}" if obj.public_description else obj.name,
            keywords=list(obj.aliases),
        )
        for obj in objects
    ]
    verdicts = await classifier.classify(entries, cards, seed_text)
    for obj, verdict in zip(objects, verdicts, strict=True):
        obj.visibility = object_visibility(verdict)
    return verdicts
