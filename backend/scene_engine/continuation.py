"""角色回复的续写截断（工单30）。

角色拿到的"目前对话"是一份逐行排列的剧本，模型说完自己这一轮后常常不停，接着替别人写台词、
写 `【环境】…` / `【旁白】…`，直到复读到输出上限。这些内容被解析进 `dialogue` 就是公开对白：
进全场 transcript 与每个在场角色的长期记忆（按正文寻址、不随回滚撤销），伪造的环境结果
因此绕过裁决器成了全员已知的事实。

这里只做一件事：在解析之前找到"开始续写"的位置并截掉其后的一切。截下的内容**不挽救**
（不当提示、不改写成旁白、不交给裁决器，工单30 R4）。判定是确定性的，30b 的回放评测
用同一个函数统计污染率，不另写一套。

续写标记：
- `【…】` 标签，但只认**已知的**：伪发言人（环境 / 旁白 / 导演…，含以它们开头的自造变体如
  `【导演视角】`）、参演角色名、我们自己 prompt 里的区块标题。**不能认任意 `【…】`**：游戏 / 奇幻类
  种子常用 `【火球术】` 标技能、物品、称号，按任意标签截会把正常台词截掉，标签在开头时整段截光，
  重新要一次仍是同样写法就抛错，场景每次续跑都卡在同一处（PR #34 评审）；
- 发言人标签：本场**其他**参演角色或伪发言人，后跟半角或全角冒号，
  且位于行首或紧跟在空白 / 句末标点之后。名字后不跟冒号（"诺安大人说得对"）不算。

本人名字标签（"塞芙拉: 诺安大人…"）不论在开头还是中途都只剥掉标签、保留正文：那是模仿剧本行
格式，正文仍是自己这一轮。模型常把一轮写成 `*动作*` 换行 `本人: 台词`，30a 起初把中途的本人标签
也当续写，结果截掉的是本人这一轮的台词（30b 回放：叙事化视图与多轮结构下尤其多）。真正"替自己写
下一轮"之前必然先写别人或环境，那里已经截断了。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from backend.models import ENVIRONMENT_SPEAKER

# 不是角色、却会被模型当成发言人写出来的名字。只认后跟冒号或包在【】里的形式，正常台词里提到它们不受影响
_PSEUDO_SPEAKERS = (ENVIRONMENT_SPEAKER, "旁白", "导演", "用户", "系统")
# 角色 prompt 里的区块标题：模型把它们当作"可以输出的标签"照抄。`test_output_boundary` 扫描
# `character_agent.py` 钉住这份名单，prompt 新增区块而这里没跟上会变红
PROMPT_SECTION_TITLES = (
    "角色设定", "外貌", "说话风格", "当前状态", "你所了解的世界", "你知道的事实", "人际关系", "当前场景",
    "在场物件", "行为格式规范", "相关记忆", "目前对话", "当前环境", "你此刻想起的",
)
_LABEL_RE = re.compile(r"【\s*([^】\n]{1,16}?)\s*】")
# 发言人标签只在行首或"上一句已经说完"之后才算：直接接在正文字词后面的不是另起一行
_SPEAKER_BOUNDARY = r"(?:^|(?<=[\s。！？!?…」”』）)\]］*]))"


def _speaker_pattern(names: Sequence[str]) -> re.Pattern[str] | None:
    unique = sorted({n.strip() for n in names if n and n.strip()}, key=len, reverse=True)
    if not unique:
        return None
    alternatives = "|".join(re.escape(n) for n in unique)
    return re.compile(rf"{_SPEAKER_BOUNDARY}(?:{alternatives})\s*[:：]", re.MULTILINE)


def _strip_own_labels(text: str, self_name: str) -> str:
    """剥掉本人名字标签（开头与中途），正文原样保留。"""
    if not self_name.strip():
        return text
    name = re.escape(self_name.strip())
    text = re.sub(rf"^\s*{name}\s*[:：]\s*", "", text, count=1)
    return re.sub(rf"{_SPEAKER_BOUNDARY}{name}\s*[:：][ \t]*", "", text, flags=re.MULTILINE)


def _is_known_label(content: str, names: Sequence[str]) -> bool:
    if content in PROMPT_SECTION_TITLES or content in names:
        return True
    # 伪发言人开头的自造变体：【导演视角】【环境描写】【系统提示】
    return content.startswith(_PSEUDO_SPEAKERS)


def trim_continuation(
    raw: str, *, self_name: str, other_names: Sequence[str]
) -> tuple[str, str]:
    """返回 (保留的本人回复, 截掉的续写)。没有续写时第二项为空串。

    保留部分可能为空（整段回复就是续写），由调用方按空回复处理 —— 不得落一个空轮次。
    本人名字标签不算续写，只剥掉（见模块说明）。
    """
    names = [n.strip() for n in (self_name, *other_names) if n and n.strip()]
    others = [n.strip() for n in other_names if n and n.strip() and n.strip() != self_name.strip()]
    text = raw
    cut_at = len(text)
    # 【本人名】仍是续写标记：剧本里没有这种写法，模型写它是在仿区块标题
    for label in _LABEL_RE.finditer(text):
        if _is_known_label(label.group(1), names):
            cut_at = label.start()
            break
    speakers = _speaker_pattern([*others, *_PSEUDO_SPEAKERS])
    if speakers is not None:
        speaker = speakers.search(text)
        if speaker and speaker.start() < cut_at:
            cut_at = speaker.start()

    return _strip_own_labels(text[:cut_at], self_name).rstrip(), text[cut_at:]
