"""角色回复的续写截断（工单30）。

角色拿到的"目前对话"是一份逐行排列的剧本，模型说完自己这一轮后常常不停，接着替别人写台词、
写 `【环境】…` / `【旁白】…`，直到复读到输出上限。这些内容被解析进 `dialogue` 就是公开对白：
进全场 transcript 与每个在场角色的长期记忆（按正文寻址、不随回滚撤销），伪造的环境结果
因此绕过裁决器成了全员已知的事实。

这里只做一件事：在解析之前找到"开始续写"的位置并截掉其后的一切。截下的内容**不挽救**
（不当提示、不改写成旁白、不交给裁决器，工单30 R4）。判定是确定性的，30b 的回放评测
用同一个函数统计污染率，不另写一套。

续写标记：
- `【任意】` 标签：环境回合与 prompt 区块标题同形，模型会自造同类标签（`【用户】` `【导演视角】`），
  所以不能只认已知的几个；角色的独白用方括号 `[]` / `［］`，不受影响；
- 发言人标签：本场参演角色（含本人）或环境、旁白之类的伪发言人，后跟半角或全角冒号，
  且位于行首或紧跟在空白 / 句末标点之后。名字后不跟冒号（"诺安大人说得对"）不算。

开头的本人名字前缀（"塞芙拉: 诺安大人…"）只剥掉前缀：那是模仿剧本行格式，正文仍是自己的台词。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from backend.models import ENVIRONMENT_SPEAKER

# 不是角色、却会被模型当成发言人写出来的名字。只认后跟冒号的形式，正常台词里提到它们不受影响
_PSEUDO_SPEAKERS = (ENVIRONMENT_SPEAKER, "旁白", "导演", "用户", "系统")
# 标签内容限长、不跨行：截的是"像标签的东西"，不是任意一对【】括起来的长文本
_LABEL_RE = re.compile(r"【[^】\n]{1,16}】")
# 发言人标签只在行首或"上一句已经说完"之后才算：直接接在正文字词后面的不是另起一行
_SPEAKER_BOUNDARY = r"(?:^|(?<=[\s。！？!?…」”』）)\]］*]))"


def _speaker_pattern(names: Sequence[str]) -> re.Pattern[str] | None:
    unique = sorted({n.strip() for n in names if n and n.strip()}, key=len, reverse=True)
    if not unique:
        return None
    alternatives = "|".join(re.escape(n) for n in unique)
    return re.compile(rf"{_SPEAKER_BOUNDARY}(?:{alternatives})\s*[:：]", re.MULTILINE)


def trim_continuation(
    raw: str, *, self_name: str, other_names: Sequence[str]
) -> tuple[str, str]:
    """返回 (保留的本人回复, 截掉的续写)。没有续写时第二项为空串。

    保留部分可能为空（整段回复就是续写），由调用方按空回复处理 —— 不得落一个空轮次。
    """
    text = raw
    if self_name:
        text = re.sub(rf"^\s*{re.escape(self_name)}\s*[:：]\s*", "", text, count=1)

    cut_at = len(text)
    label = _LABEL_RE.search(text)
    if label:
        cut_at = label.start()
    speakers = _speaker_pattern([self_name, *other_names, *_PSEUDO_SPEAKERS])
    if speakers is not None:
        speaker = speakers.search(text)
        if speaker and speaker.start() < cut_at:
            cut_at = speaker.start()

    return text[:cut_at].rstrip(), text[cut_at:]
