"""SummaryAgent：总结智能体。

根据场景日志生成梗概与多格式输出（网文/剧本/舞台剧/报告/原始日志）。
temperature 默认 0.7。
"""

from __future__ import annotations

import json

from backend.config import settings
from backend.models import DialogueTurn, OutputFormat, Scene
from backend.utils.llm import chat_safe
from backend.utils.logger import get_logger
from backend.utils.serializer import to_dict

logger = get_logger("agents.summary")


_FORMAT_INSTRUCTIONS = {
    OutputFormat.WEB_NOVEL.value: (
        "请改写为流畅的网络小说，第三人称叙事，将动作与对白自然融入叙述，"
        "营造画面感与情绪张力。不要逐字罗列内心独白，而是转化为叙事描写。"
    ),
    OutputFormat.SCREENPLAY.value: (
        "请改写为标准影视剧本格式：\n"
        "场景行用『场景 - 地点 - 时间』；\n"
        "人物名居中大写后接对白；\n"
        "动作描述用括号或独立段落。"
    ),
    OutputFormat.STAGE_PLAY.value: (
        "请改写为舞台剧本格式，包含【幕】【场】、人物上下场提示、"
        "舞台指示（用方括号），以及人物对白。"
    ),
    OutputFormat.SUMMARY_REPORT.value: (
        "请生成推演分析报告：包括剧情梗概、关键转折、人物动机分析、"
        "戏剧冲突评估，以分析性、客观的语气书写。"
    ),
}


def _format_transcript(log: list[DialogueTurn], include_thoughts: bool = False) -> str:
    lines = []
    for t in log:
        parts = []
        if t.action:
            parts.append(f"*{t.action}*")
        if t.dialogue:
            parts.append(t.dialogue)
        if include_thoughts and t.inner_thought:
            parts.append(f"[{t.inner_thought}]")
        lines.append(f"{t.character_name}: {' '.join(parts)}")
    return "\n".join(lines)


class SummaryAgent:
    """总结智能体。"""

    def __init__(self, temperature: float = 0.7):
        self.temperature = temperature
        self.model = settings.summary_model

    async def generate_synopsis(
        self,
        scenes: list[Scene],
        style: str = "narrative",
    ) -> str:
        """生成梗概，供导演评估参考。"""
        transcript = "\n\n".join(
            f"== 场景：{s.name} ==\n{_format_transcript(s.dialogue_log)}" for s in scenes
        )
        style_hint = {
            "narrative": "用连贯叙事概括",
            "bullet": "用要点列表概括",
            "timeline": "用时间线形式概括",
        }.get(style, "用连贯叙事概括")
        prompt = (
            f"请{style_hint}以下剧情场景的梗概（200字以内）：\n\n{transcript[:8000]}"
        )
        return await chat_safe([{"role": "user", "content": prompt}], temperature=self.temperature, model=self.model)

    async def generate_output(
        self,
        scenes: list[Scene],
        output_format: OutputFormat,
        branch_id: str | None = None,
    ) -> str:
        """生成最终输出文本。"""
        if branch_id:
            scenes = [s for s in scenes if s.branch_id == branch_id]

        if output_format == OutputFormat.RAW_LOG:
            return json.dumps(
                [to_dict(s) for s in scenes], ensure_ascii=False, indent=2
            )

        # 非 raw 格式：默认不暴露角色内心独白（CLAUDE.md 10.6）
        transcript = "\n\n".join(
            f"== 场景：{s.name}（{s.location}）==\n{s.description}\n"
            f"{_format_transcript(s.dialogue_log, include_thoughts=False)}"
            for s in scenes
        )
        instruction = _FORMAT_INSTRUCTIONS.get(
            output_format.value, _FORMAT_INSTRUCTIONS[OutputFormat.WEB_NOVEL.value]
        )
        prompt = (
            f"{instruction}\n\n以下是原始场景日志：\n\n{transcript[:12000]}"
        )
        return await chat_safe([{"role": "user", "content": prompt}], temperature=self.temperature, model=self.model)
