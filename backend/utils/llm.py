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


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
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
) -> str:
    """发起一次对话补全，返回纯文本内容。带重试。"""
    try:
        resp = await _client().chat.completions.create(
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
) -> str:
    """带兜底的对话调用：失败时抛出 LLMError 而非原始异常。"""
    try:
        return await chat(messages, temperature=temperature, model=model)
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"LLM 调用最终失败：{exc}") from exc
