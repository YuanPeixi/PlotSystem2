"""智能体公共基类。封装 AutoGen 模型客户端的创建。"""

from __future__ import annotations

from backend.config import settings
from backend.utils.logger import get_logger

logger = get_logger("agents.base")

try:  # pragma: no cover
    from autogen_ext.models.openai import OpenAIChatCompletionClient

    _AUTOGEN_AVAILABLE = True
except Exception:  # noqa: BLE001
    OpenAIChatCompletionClient = None  # type: ignore[assignment]
    _AUTOGEN_AVAILABLE = False


def autogen_available() -> bool:
    return _AUTOGEN_AVAILABLE


def make_model_client(temperature: float = 0.7, model: str | None = None):
    """创建 AutoGen OpenAI 兼容模型客户端。

    使用 model_info 显式声明能力，以兼容非 OpenAI 官方模型（如 qwen）。

    Args:
        model: 具体模型 ID。留空回退到 settings.LLM_MODEL_NAME。
            调用方若需要异构模型（导演/角色/总结分别用不同模型），
            必须显式传入 settings.director_model / character_model / summary_model，
            否则会静默退化为统一模型——这里不做隐式判断。

    ⚠️ 路径说明（2026-07-15）：本函数及其唯一调用方 CharacterAgent.get_autogen_agent()
    目前【没有被 SceneEngine 实际调用】——真实运行路径是 CharacterAgent.respond()
    直连 chat_safe()，不经过 AutoGen 运行时。这条 AutoGen 集成路径是为未来可能的
    Selector/工具调用式环境交互预留的半成品。若要启用它，请先看
    docs/fix-tickets/11-selector-and-world-interaction.md，并重新确认
    GroupChat/工具调用相关的落地方案，不要假设这里已经跑通过。
    """
    if not _AUTOGEN_AVAILABLE:
        raise RuntimeError("autogen-ext 未安装，无法创建模型客户端。")
    return OpenAIChatCompletionClient(
        model=model or settings.LLM_MODEL_NAME,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        temperature=temperature,
        model_info={
            "vision": False,
            "function_calling": False,
            "json_output": False,
            "family": "unknown",
            "structured_output": False,
        },
    )


class BaseAgent:
    """所有业务智能体的基类。"""

    def __init__(self, name: str, temperature: float = 0.7):
        self.name = name
        self.temperature = temperature
