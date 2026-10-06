"""环境裁决器（工单20 PR-2a，揭示来源 ① ③）。

角色写下"尝试"，这里按物件的隐藏规则裁决结果：能不能做、触发了什么、在场的人看见什么、
当事人独自察觉到什么、物件状态怎么变。物件是被动规则，不发言、没有意图（设计单 §2）。

隔离（契约1，设计单 R1 / R2）：
- 输入只有物件（公开描述 + 隐藏规则 + 当前状态）、导演为这个物件预制的揭示、动作原文与执行者名。
  **不含任何角色的视图**（已知事实、未知事实、记忆、关系）—— 裁决器不需要知道谁知道什么，
  给了反而可能把别人的秘密写进叙述；
- 输出的 `narration` 是公开的，进全场 transcript 与每个在场角色的记忆。提示词要求它只写
  可观察现象；`private_detail` 只在导演预制了揭示（①）且规则触发时才有，必须是预制揭示的
  原文摘录（解析时校验），且只进执行者本人的记忆。
  ② 现场生成要拆成独立调用（A13），归 20b，这里不产出。

调用走 `chat_safe`（契约7），用导演模型、温度 0.3（要一致，不要创意）。
"""

from __future__ import annotations

from backend.config import settings
from backend.exceptions import LLMError
from backend.models import ActionIntent, LLMPurpose, WorldObject
from backend.services.environment import ENVIRONMENT_ATTR_CHARS, Adjudication, parse_adjudication
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

【导演为这个物件预制的揭示】
{script}

【动作】{actor} 尝试：{text}
（动词：{verb}；意图：{detail}）

裁决要求：
1. executable：这个动作在物理上能不能做到。做不到时 triggered 必须为 false、不改状态；
2. triggered：只按上面的隐藏规则判断是否触发，**不得自创规则**。条件没满足（比如只是碰了一下、
   方法不对、不是规则要求的人）就是没触发 —— 没触发也要写叙述，说明物件的实际反应（例如"冰凉，毫无动静"）；
3. narration：在场所有人都能看见、听见的现象，一两句，第三人称。**不得写出隐藏规则本身，
   不得写任何人的内心、身世或秘密**，只写可观察到的事；
4. reveal_source 与 private_detail：规则触发后若要揭示只有执行者本人能感知的内容（例如看见的幻象），
   **只能从上面的导演预制揭示里原样摘录与本次动作对应的那一段，一字不改**（不要换人称、不要概括、
   不要补充），此时 reveal_source 为 "script"。不是原文摘录的私密细节会被丢弃。
   没触发、没有预制揭示或用不上时，reveal_source 为 "observable"、private_detail 留空，只在 narration 里写可观察现象；
5. state_changes：这个物件在场所有人都看得出的状态变化，键为属性名（如"佩戴者""光芒""暗格"），
   不超过 {attr_chars} 字；某属性不再成立时值为 null。没有变化给 {{}}。只能改这个物件。

只输出一个 JSON 对象，不要任何解释：
{{"executable": true, "triggered": false, "reveal_source": "observable", "narration": "……", "private_detail": "", "state_changes": {{}}}}"""


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def build_adjudication_prompt(
    obj: WorldObject,
    actor_name: str,
    intent: ActionIntent,
    state: dict[str, str],
    script: str,
) -> str:
    """组装裁决 prompt。参数就是裁决器能看到的全部 —— 没有任何角色视图的入口（R1 / R2）。"""
    rules ="\n".join(f"- {_one_line(r)}" for r in obj.hidden_rules if _one_line(r)) or "（无特殊规则）"
    state_text = "\n".join(f"- {_one_line(k)}：{_one_line(v)}" for k, v in state.items()) or "（无记录，按公开描述）"
    return _PROMPT.format(
        name=_one_line(obj.name),
        description=_one_line(obj.public_description) or "（无描述）",
        rules=rules,
        state=state_text,
        script=_one_line(script) or "（无）",
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
        script: str = "",
    ) -> Adjudication | None:
        """裁决一个动作。None = 裁决失败（调用失败或输出不可用），调用方记 failed、不生成环境回合。"""
        prompt = build_adjudication_prompt(obj, actor_name, intent, state, script)
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
        result = parse_adjudication(raw, script=script)
        if result is None:
            logger.warning("环境裁决的输出不可用（物件 %s、执行者 %s）：%s", obj.name, actor_name, raw[:300])
            return None
        if result.rejected:
            logger.warning("环境裁决（物件 %s）有 %d 处被规整：%s",
                           obj.name, len(result.rejected), "；".join(result.rejected))
        return result
