"""统一 LLM 调用封装。

所有 LLM 调用走这里：统一配置、超时、重试（最多3次）。
使用 OpenAI 兼容 SDK，可对接任意 OpenAI 格式 API。
"""

from __future__ import annotations

from openai import AsyncOpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from backend.config import settings
from backend.exceptions import LLMError
from backend.utils.logger import get_logger

logger = get_logger("llm")

_REQUEST_TIMEOUT = 180.0


def _client(base_url: str | None = None, api_key: str | None = None) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=api_key or settings.LLM_API_KEY,
        base_url=base_url or settings.LLM_BASE_URL,
        timeout=_REQUEST_TIMEOUT,
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def chat(
    messages: list[dict],
    *,
    temperature: float = 0.7,
    model: str | None = None,
    max_tokens: int | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> str:
    """发起一次对话补全，返回纯文本内容。带重试。

    base_url / api_key 留空即用全局配置；传入时可把某类调用（如 selector
    打分）路由到另一个服务商或本地模型，同时保持本模块为唯一出口。
    """
    try:
        resp = await _client(base_url, api_key).chat.completions.create(
            model=model or settings.LLM_MODEL_NAME,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM 调用失败，将重试：%s", exc)
        raise


async def chat_safe(
    messages: list[dict],
    *,
    temperature: float = 0.7,
    model: str | None = None,
    max_tokens: int | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> str:
    """带兜底的对话调用：失败时抛出 LLMError 而非原始异常。"""
    try:
        return await chat(
            messages,
            temperature=temperature,
            model=model,
            max_tokens=max_tokens,
            base_url=base_url,
            api_key=api_key,
        )
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"LLM 调用最终失败：{exc}") from exc


def estimate_tokens(text: str) -> int:
    """粗略估算文本 token 数（不引入 tiktoken 等新依赖）。

    经验规则：CJK 字符约 1 token/字，其余字符（英文/数字/标点）约 4 字符/token。
    仅用于上下文预算控制，允许一定误差，宁可高估不可低估。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + (len(text) - cjk) // 4 + 1
