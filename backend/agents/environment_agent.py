"""环境裁决器（工单20 PR-2a，揭示来源 ① ③）。

角色写下"尝试"，这里按物件的隐藏规则裁决结果：能不能做、触发了什么、在场的人看见什么、
命中了哪条导演预制揭示、物件状态怎么变。物件是被动规则，不发言、没有意图（设计单 §2）。

隔离（契约1，设计单 R1 / R2 / A31）：
- 输入只有物件（公开描述 + 隐藏规则 + 当前状态）、导演预制揭示的**触发情形**、动作原文与
  执行者名。**不含任何角色的视图**（已知事实、未知事实、记忆、关系）—— 裁决器不需要知道
  谁知道什么，给了反而可能把别人的秘密写进叙述；
- 预制揭示的**内容**不进这个 prompt：裁决器只回答命中第几条，内容由解析时原样取出、只进
  执行者本人的记忆。它看不见内容，公开叙述就无从转述它；
- 输出的 `narration` 是公开的，进全场 transcript 与每个在场角色的记忆。提示词要求它只写
  可观察现象 —— 这一条只能靠提示词（叙述本来就要现场写）。
  ② 现场生成要拆成独立调用（A13），归 20b，这里不产出。

调用走 `chat_safe`（契约7），用导演模型、温度 0.3（要一致，不要创意）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from backend.config import settings
from backend.exceptions import InvalidRequestError, LLMError
from backend.models import (
    ActionIntent,
    LLMPurpose,
    RevealEntry,
    WorldAdjudication,
    WorldObject,
    WorldRevision,
)
from backend.services.environment import (
    ENVIRONMENT_ATTR_CHARS,
    Adjudication,
    normalize_reveals,
    parse_adjudication,
)
from backend.services.world import object_state, parse_operations
from backend.utils.llm import chat_safe
from backend.utils.logger import get_logger

logger = get_logger("agents.environment")

_TEMPERATURE = 0.3
_MAX_TOKENS = 600

_PROMPT = """你是剧情推演里的环境裁决者。物件不会说话、没有意图，它只按自己的规则对角色的动作作出反应。
请裁决下面这个动作的结果。

【物件】{name}
公开描述：{description}
隐藏规则（只有你知道，角色不知道）：
{rules}

【物件当前状态】
{state}

【导演预制的揭示（只列触发情形，内容由系统交给执行者）】
{reveals}

【动作】{actor} 尝试：{text}
（动词：{verb}；意图：{detail}）

裁决要求：
1. executable：这个动作在物理上能不能做到。做不到时 triggered 必须为 false、不改状态；
2. triggered：只按上面的隐藏规则判断是否触发，**不得自创规则**。条件没满足（比如只是碰了一下、
   方法不对、不是规则要求的人）就是没触发 —— 没触发也要写叙述，说明物件的实际反应（例如"冰凉，毫无动静"）；
3. narration：在场所有人都能看见、听见的现象，一两句，第三人称。**不得写出隐藏规则本身，
   不得写任何人的内心、身世或秘密**，只写可观察到的事；
4. reveal_index：规则触发、且本次动作与上面某条预制揭示的触发情形相符时，给出那一条的编号；
   没触发、没有相符的、或没有预制揭示时给 0。**只能选一条**，执行者是谁就选与他相符的那条；
5. state_changes：这个物件在场所有人都看得出的状态变化，键为属性名（如"佩戴者""光芒""暗格"），
   不超过 {attr_chars} 字；某属性不再成立时值为 null。没有变化给 {{}}。只能改这个物件。

只输出一个 JSON 对象，不要任何解释：
{{"executable": true, "triggered": false, "reveal_index": 0, "narration": "……", "state_changes": {{}}}}"""


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def build_adjudication_prompt(
    obj: WorldObject,
    actor_name: str,
    intent: ActionIntent,
    state: dict[str, str],
    reveals: Sequence[RevealEntry],
) -> str:
    """组装裁决 prompt。参数就是裁决器能看到的全部 —— 没有任何角色视图的入口（R1 / R2）。

    `reveals` 必须已经过 `normalize_reveals`，且与解析时用的是同一份：编号要对得上。
    只渲染触发情形，不渲染内容。
    """
    rules = "\n".join(f"- {_one_line(r)}" for r in obj.hidden_rules if _one_line(r)) or "（无特殊规则）"
    state_text = "\n".join(f"- {_one_line(k)}：{_one_line(v)}" for k, v in state.items()) or "（无记录，按公开描述）"
    reveal_text = "\n".join(f"{i}. {r.condition}" for i, r in enumerate(reveals, start=1)) or "（无）"
    return _PROMPT.format(
        name=_one_line(obj.name),
        description=_one_line(obj.public_description) or "（无描述）",
        rules=rules,
        state=state_text,
        reveals=reveal_text,
        actor=_one_line(actor_name),
        text=_one_line(intent.text),
        verb=_one_line(intent.verb) or "（未识别）",
        detail=_one_line(intent.detail) or "（未识别）",
        attr_chars=ENVIRONMENT_ATTR_CHARS,
    )


class EnvironmentAgent:
    """每场一个实例，由 orchestrator 构造后注入引擎（R8：引擎不碰 SQLite，也不自己建 LLM 客户端）。"""

    def __init__(self, model: str | None = None, temperature: float = _TEMPERATURE):
        self.model = model or settings.director_model
        self.temperature = temperature

    async def adjudicate(
        self,
        obj: WorldObject,
        actor_name: str,
        intent: ActionIntent,
        state: dict[str, str],
        reveals: Sequence[RevealEntry] = (),
    ) -> Adjudication | None:
        """裁决一个动作。None = 裁决失败（调用失败或输出不可用），调用方记 failed、不生成环境回合。"""
        # prompt 与解析用同一份规整结果，编号才指向同一条
        entries = normalize_reveals(reveals)
        prompt = build_adjudication_prompt(obj, actor_name, intent, state, entries)
        try:
            raw = await chat_safe(
                [{"role": "user", "content": prompt}],
                temperature=self.temperature,
                model=self.model,
                max_tokens=_MAX_TOKENS,
                purpose=LLMPurpose.ADJUDICATE,
            )
        except LLMError as exc:
            logger.warning("环境裁决调用失败（物件 %s、执行者 %s）：%s", obj.name, actor_name, exc)
            return None
        result = parse_adjudication(raw, reveals=entries)
        if result is None:
            logger.warning("环境裁决的输出不可用（物件 %s、执行者 %s）：%s", obj.name, actor_name, raw[:300])
            return None
        if result.rejected:
            logger.warning("环境裁决（物件 %s）有 %d 处被规整：%s",
                           obj.name, len(result.rejected), "；".join(result.rejected))
        return result


    async def adjudicate_world(
        self, obj: WorldObject, actor_id: str, actor_name: str, intent: ActionIntent,
        revision: WorldRevision, *, reveals: Sequence[RevealEntry] = (),
        actor_material: str = "", allowed_object_ids: set[str] | None = None,
    ) -> WorldAdjudication | None:
        allowed = allowed_object_ids or {obj.object_id}
        entries = normalize_reveals(reveals)
        # Related state is allowed; unrelated definitions and all character secrets are absent.
        payload = {
            "actor": {"id": actor_id, "name": actor_name}, "action": intent.text,
            "target": {"id": obj.object_id, "name": obj.name, "description": obj.public_description,
                       "hidden_rules": obj.hidden_rules},
            "allowed_objects": [{"id": oid, "name": revision.definitions[oid].name,
                                 "state": object_state(revision, oid)}
                                for oid in sorted(allowed) if oid in revision.definitions],
            "reveals": [{"index": i, "condition": r.condition} for i, r in enumerate(entries, 1)],
        }
        try:
            raw = await chat_safe(
                [{"role": "user", "content": _UNIFIED_PROMPT.format(payload=json.dumps(payload, ensure_ascii=False))}],
                temperature=self.temperature, model=self.model, max_tokens=1800, purpose=LLMPurpose.ADJUDICATE,
            )
            parsed = parse_world_adjudication(raw, allowed)
            if parsed is None:
                logger.warning("统一环境裁决输出不可用（物件 %s）", obj.name)
                return None
            result, index, effect = parsed
            if index:
                if index > len(entries):
                    return None
                result.private_detail = entries[index - 1].content
            elif effect and actor_material:
                # Separate call: it receives no hidden rules, script or other characters' information.
                try:
                    reveal = await chat_safe(
                        [{"role": "user", "content": "只根据当事人已知材料生成私密揭示，不编造新事实。"
                          "材料不足只写可感知现象。只输出一小段文字。\n效果：投射当事人自己已知的一段记忆。"
                          + "\n当事人材料：\n" + actor_material}],
                        temperature=self.temperature, model=self.model, max_tokens=300, purpose=LLMPurpose.ADJUDICATE,
                    )
                    result.private_detail = reveal.strip()
                except LLMError as exc:
                    # Public adjudication remains valid even when its private reveal cannot be generated.
                    logger.warning("私密揭示生成失败，保留已裁决的公开结果：%s", exc)
            return result
        except LLMError as exc:
            logger.warning("统一环境裁决失败（物件 %s）：%s", obj.name, exc)
            return None


_UNIFIED_PROMPT = """你是剧情推演的环境裁决者。只按给定规则裁决一次尝试。
输入数据：
{payload}

要求：
- executable 表示物理上能否执行；triggered 只按隐藏规则判定，不创造规则。
- narration 只写所有在场者能观察到的现象，不泄露隐藏规则、秘密或内心。
- operations 是已发生的物件属性变化，不是言论、猜测或知情声明。
- 只改变本次允许的物件。每项给稳定 attribute（重复动作复用既有属性标识），可选中文 label。
- 持有者、位置等实体引用使用 {{"kind":"character|object|location","id":"稳定ID"}}。
- bool 和数字保持类型；set 写值，unset 清除属性；不根据公开描述推翻已有属性。
- 每项 subject_kind 固定 object，visibility=observable 只放现场能看出的变化；不可观察的内部状态用 hidden，
  不在 narration 中透露隐藏属性名或值。最多 32 项。不因展示预算省略事实。
- reveal_index 只选择匹配的预制揭示编号，未匹配给 0。你看不到揭示内容。
- reveal_effect 只有触发当事人记忆揭示时给固定代码 actor_memory，否则空串。禁止任何自由文本或秘密。
输出一个 JSON 对象：
{{"executable":true,"triggered":false,"narration":"…","reveal_index":0,"reveal_effect":"",
"operations":[{{"op":"set","subject_kind":"object","subject_id":"ID","attribute":"open",
"label":"开启","value":true,"visibility":"observable"}}]}}
"""


def parse_world_adjudication(raw: str, allowed_objects: set[str]) -> tuple[WorldAdjudication, int, str] | None:
    try:
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        executable = data.get("executable")
        triggered = data.get("triggered")
        narration = data.get("narration")
        if type(executable) is not bool or type(triggered) is not bool:
            return None
        if not isinstance(narration, str) or not narration.strip() or len(narration) > 2000:
            return None
        operations = parse_operations(data.get("operations", []))
        if not executable and (triggered or operations):
            return None
        if any(o.subject_kind != "object" or o.subject_id not in allowed_objects
               or o.visibility not in {"observable", "hidden"} for o in operations):
            return None
        index = data.get("reveal_index", 0)
        effect = data.get("reveal_effect", "")
        if type(index) is not int or index < 0 or not isinstance(effect, str) or effect not in {"", "actor_memory"}:
            return None
        if not triggered and (index or effect):
            return None
        return WorldAdjudication(executable=executable, triggered=triggered,
                                 narration=" ".join(narration.split()), operations=operations), index, effect
    except (ValueError, TypeError, KeyError, InvalidRequestError):
        return None
