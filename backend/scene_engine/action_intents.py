"""动作意图：预过滤 + 抽取（工单24 PR-1b）。

每个 `*动作*` 段生成一个 `ActionIntent`。绝大多数段落是神态（*皱眉*），本地子串匹配就能
排除，零 LLM 调用；提到本场在场物件的段落才进意图抽取，**一轮最多一次调用**。

隔离（契约1，设计单 R1 / A19）：
- 抽取 prompt 只有执行者名、命中段、候选物件的名称与公开描述。**不给隐藏规则**（给了模型就能
  预判结果），**不给对白**（角色把 `[独白]` 格式写坏时，独白会混在对白里，字段级断言挡不住）；
- 段内的 `[...]` 在生成 `text` 之前剥掉：动作正则会把 `*走向王冠[她想戴上]*` 整段捕获，
  而 `text` / `detail` 在 PR-2 会交给裁决器、裁决器的输出是公开叙述。

确定性（设计单 A18）：命中段按段落顺序取前 `MAX_ACTION_EXTRACTS_PER_TURN` 段，结果按 `index`
回填，不依赖模型返回的顺序。抽取在第一次落盘之前完成，落盘的轮次意图恒完整，续跑不重抽。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from backend.config import settings
from backend.exceptions import LLMError
from backend.models import (
    ActionIntent,
    ActionSkipReason,
    ActionStatus,
    LLMPurpose,
    WorldObject,
)
from backend.utils.llm import chat_safe
from backend.utils.logger import get_logger

logger = get_logger("scene_engine.actions")

#: 一轮最多送去抽取的命中段。角色一轮写六个以上针对物件的动作已属罕见，超出的标 over_limit
MAX_ACTION_EXTRACTS_PER_TURN = 6
VERB_CHARS = 8
DETAIL_CHARS = 80
_EXTRACT_MAX_TOKENS = 400
_EXTRACT_TEMPERATURE = 0.2

# 闭合的半角/全角方括号，以及写坏了没闭合的 `[` 到段尾。宁可多剥一截动作，不放过一句独白
_BRACKETED = re.compile(r"[\[［][^\]］]*[\]］]")
_UNCLOSED = re.compile(r"[\[［].*$", re.DOTALL)

_PROMPT = """你是动作意图识别器。下面是角色「{actor}」在一轮表演里的动作描写，以及其中提到的物件。
逐条判断：这段动作是不是在**对某个物件做尝试**（触碰、拿起、戴上、打开、按下、推动、使用……）。
只是看着、谈论、想起、经过某物，都不算尝试。

【物件】
{objects}

【动作】
{segments}

只输出一个 JSON 数组，每段动作一项，不要任何解释：
[{{"index": 0, "object": "o1", "is_attempt": true, "verb": "戴上", "detail": "把王冠戴到自己头上"}}]
不构成尝试时 is_attempt 为 false、object 留空。verb 不超过 {verb_chars} 字，detail 不超过 {detail_chars} 字。"""


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def strip_thoughts(segment: str) -> str:
    """剥掉段内的独白：闭合的 `[...]` / `［...］`，以及没闭合的 `[` 到段尾。"""
    return _one_line(_UNCLOSED.sub("", _BRACKETED.sub("", segment)))


def _terms(obj: WorldObject) -> list[str]:
    # 拉丁字母不区分大小写；中文 casefold 不变。单字与角色同名的别名在读写两侧都已挡掉
    return [t.casefold() for t in dict.fromkeys([obj.name, *obj.aliases]) if t]


def match_objects(text: str, objects: Sequence[WorldObject]) -> list[WorldObject]:
    """`text` 里提到的物件，按 `objects` 的顺序。不做指代消解（"把它戴上"不命中，设计单 §3）。"""
    folded = text.casefold()
    return [o for o in objects if any(t in folded for t in _terms(o))]


def build_prompt(
    actor: str, segments: Sequence[tuple[int, str]], candidates: Sequence[WorldObject]
) -> tuple[str, dict[str, str]]:
    """返回 (prompt, 代号 → object_id)。用 o1/o2 而不是 UUID：省 token，越界的代号也一眼可辨。"""
    codes = {f"o{i + 1}": o.object_id for i, o in enumerate(candidates)}
    lines = [
        f"{code} {_one_line(o.name)}：{_one_line(o.public_description) or '（无描述）'}"
        for code, o in zip(codes, candidates, strict=True)
    ]
    prompt = _PROMPT.format(
        actor=_one_line(actor),
        objects="\n".join(lines),
        segments="\n".join(f"{index}. {text}" for index, text in segments),
        verb_chars=VERB_CHARS,
        detail_chars=DETAIL_CHARS,
    )
    return prompt, codes


def _extract_array(raw: str) -> list | None:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def _is_true(value: object) -> bool:
    # 拿不准就不算尝试：误判成尝试会在 PR-2 凭空触发一次裁决
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def apply_extraction(
    intents: dict[int, ActionIntent],
    raw: str,
    codes: dict[str, str],
    attempt_status: str = ActionStatus.RECORDED.value,
) -> None:
    """把模型输出按 `index` 回填进送去抽取的那几段（就地修改）。

    同一 index 只认第一条；index 越界、不是整数的条目丢弃；没被任何条目覆盖的段落记
    `invalid_object` —— 模型漏答不等于"不是尝试"，不能替它下结论。
    构成尝试的段落置 `attempt_status`：record 档 recorded，adjudicate 档 pending（待裁决）。
    """
    items = _extract_array(raw)
    if items is None:
        for intent in intents.values():
            intent.skip_reason = ActionSkipReason.EXTRACT_FAILED.value
        logger.warning("动作意图抽取的输出无法解析，本轮 %d 段按失败处理", len(intents))
        return
    seen: set[int] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index not in intents or index in seen:
            continue
        seen.add(index)
        intent = intents[index]
        if not _is_true(item.get("is_attempt")):
            intent.skip_reason = ActionSkipReason.NOT_ATTEMPT.value
            continue
        object_id = codes.get(str(item.get("object") or "").strip())
        if object_id is None:
            intent.skip_reason = ActionSkipReason.INVALID_OBJECT.value
            continue
        intent.object_id = object_id
        intent.verb = _one_line(item.get("verb") or "")[:VERB_CHARS]
        intent.detail = _one_line(item.get("detail") or "")[:DETAIL_CHARS]
        intent.status = attempt_status
        intent.skip_reason = ""
    for index, intent in intents.items():
        if index not in seen:
            intent.skip_reason = ActionSkipReason.INVALID_OBJECT.value


class ActionIntentExtractor:
    """每场一个实例，持有本场在场物件。`ENVIRONMENT_MODE` 为 record 或 adjudicate 时构造。

    两档只差尝试的去向：record 档置 recorded、不裁决；adjudicate 档置 pending 待裁决，
    并受本场环境回合额度约束（设计单 A15）。
    """

    def __init__(self, objects: Sequence[WorldObject], *, adjudicate: bool = False):
        self.objects = list(objects)
        self.adjudicate = adjudicate

    async def extract(
        self, raw_segments: Sequence[str], actor: str, quota: int | None = None
    ) -> list[ActionIntent]:
        """`raw_segments` 是动作正则按段落顺序抓到的原文，index 即其下标。

        `quota` 只在 adjudicate 档有意义：本场还能产生几个环境回合（已扣掉日志里的环境回合与
        仍 pending 的动作）。<= 0 时命中段直接记 quota、**不发起抽取**——放到裁决之后再判，
        到顶后每个命中轮次都要白付一次抽取 + 一次裁决再丢弃结果。抽出的尝试按段落顺序
        占额度，超出的记 quota。
        """
        intents = [
            ActionIntent(
                index=i,
                text=strip_thoughts(seg),
                skip_reason=ActionSkipReason.NO_OBJECT.value,
            )
            for i, seg in enumerate(raw_segments)
        ]
        hits: list[tuple[ActionIntent, list[WorldObject]]] = []
        for intent in intents:
            matched = match_objects(intent.text, self.objects) if intent.text else []
            if matched:
                hits.append((intent, matched))
        if not hits:
            return intents

        if self.adjudicate and quota is not None and quota <= 0:
            for intent, _ in hits:
                intent.skip_reason = ActionSkipReason.QUOTA.value
            logger.warning("本场环境回合额度已满，角色 %s 本轮 %d 段提到物件的动作不再识别", actor, len(hits))
            return intents

        sent = hits[:MAX_ACTION_EXTRACTS_PER_TURN]
        for intent, _ in hits[MAX_ACTION_EXTRACTS_PER_TURN:]:
            intent.skip_reason = ActionSkipReason.OVER_LIMIT.value
        if len(hits) > len(sent):
            logger.warning(
                "角色 %s 本轮有 %d 段动作提到物件，超过上限 %d，其余不抽取",
                actor, len(hits), MAX_ACTION_EXTRACTS_PER_TURN,
            )

        # 候选只给送去抽取的段落里提到的物件，按本场物件顺序排列 —— 代号确定性
        mentioned = {o.object_id for _, matched in sent for o in matched}
        candidates = [o for o in self.objects if o.object_id in mentioned]
        prompt, codes = build_prompt(actor, [(i.index, i.text) for i, _ in sent], candidates)
        pending = {i.index: i for i, _ in sent}
        try:
            raw = await chat_safe(
                [{"role": "user", "content": prompt}],
                temperature=_EXTRACT_TEMPERATURE,
                model=settings.selector_model,
                max_tokens=_EXTRACT_MAX_TOKENS,
                base_url=settings.selector_base_url,
                api_key=settings.selector_api_key,
                purpose=LLMPurpose.ACTION_EXTRACT,
            )
        except LLMError as exc:
            # 失败不伪造结果：等价于"没识别出尝试"，场景照常往下演
            logger.warning("动作意图抽取失败，本轮 %d 段按失败处理：%s", len(pending), exc)
            for intent in pending.values():
                intent.skip_reason = ActionSkipReason.EXTRACT_FAILED.value
            return intents
        attempt = ActionStatus.PENDING.value if self.adjudicate else ActionStatus.RECORDED.value
        apply_extraction(pending, raw, codes, attempt)
        if self.adjudicate and quota is not None:
            attempts = [i for i in intents if i.status == ActionStatus.PENDING.value]
            for intent in attempts[max(quota, 0):]:
                # 抽取结果（物件 / 动词 / 细节）留着：action_stats 据此能看出被额度挡掉的是什么
                intent.status = ActionStatus.SKIPPED.value
                intent.skip_reason = ActionSkipReason.QUOTA.value
            if len(attempts) > quota:
                logger.warning("本场环境回合额度只剩 %d，角色 %s 本轮有 %d 个尝试不裁决",
                               quota, actor, len(attempts) - quota)
        return intents
