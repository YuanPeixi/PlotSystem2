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


def make_model_client(temperature: float = 0.7):
    """创建 AutoGen OpenAI 兼容模型客户端。

    使用 model_info 显式声明能力，以兼容非 OpenAI 官方模型（如 qwen）。
    """
    if not _AUTOGEN_AVAILABLE:
        raise RuntimeError("autogen-ext 未安装，无法创建模型客户端。")
    return OpenAIChatCompletionClient(
        model=settings.LLM_MODEL_NAME,
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
