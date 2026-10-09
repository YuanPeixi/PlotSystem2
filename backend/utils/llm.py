"""统一 LLM 调用封装。

所有 LLM 调用走这里：统一配置、超时、重试（最多3次）。
使用 OpenAI 兼容 SDK，可对接任意 OpenAI 格式 API。

调用计数（工单25）也只在这里：调用方用 `purpose` 标明用途，计数归到当前上下文
装着的计数器（`utils/usage.py`）。计数本身出错只记日志，不影响调用结果。
"""

from __future__ import annotations

import time

from openai import AsyncOpenAI
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from backend.config import settings
from backend.exceptions import LLMError
from backend.models import LLMPurpose
from backend.utils.logger import get_logger
from backend.utils.usage import current_meter, purpose_key, token_count

logger = get_logger("llm")

_REQUEST_TIMEOUT = 180.0


class EmptyCompletionError(Exception):
    """服务商返回了成功响应，正文却为空（工单30）。

    以前它被 `content or ""` 当成功交回，角色轮次就落成对白、动作、独白三项全空的一轮。
    重试耗尽由 `chat_safe` 转成 `LLMError`，与其他调用失败同一语义，调用方各走既有兜底。

    `retryable` 区分两种空：`finish_reason=length` 是推理把 `max_tokens` 吃光了，同一个 prompt
    再发几次结果一样，只会让小额度的调用（selector 打分 200、意图抽取 400）每次白等退避、
    花三倍 token（PR #34 评审）—— 不重试，立即失败；其余（服务商抖动）照常退避重试。
    """

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def _should_retry(exc: BaseException) -> bool:
    return not isinstance(exc, EmptyCompletionError) or exc.retryable


def _client(base_url: str | None = None, api_key: str | None = None) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=api_key or settings.LLM_API_KEY,
        base_url=base_url or settings.LLM_BASE_URL,
        timeout=_REQUEST_TIMEOUT,
    )


def _note_retry(state: RetryCallState) -> None:
    """tenacity 决定重试时回调：失败的尝试后面还跟着一次重试，才算一次重试。"""
    meter = current_meter()
    if meter is not None:
        meter.record_retry(purpose_key(state.kwargs.get("purpose")))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception(_should_retry),
    reraise=True,
    before_sleep=_note_retry,
)
async def _complete(
    messages: list[dict],
    *,
    temperature: float,
    model: str | None,
    max_tokens: int | None,
    base_url: str | None,
    api_key: str | None,
    purpose: str,
) -> tuple[str, object]:
    """一次带重试的补全，连同服务商返回的 usage 一起交回（可能为 None）。"""
    try:
        resp = await _client(base_url, api_key).chat.completions.create(
            model=model or settings.LLM_MODEL_NAME,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choice = resp.choices[0]
        text = choice.message.content or ""
        finish = getattr(choice, "finish_reason", None)
        if not text.strip():
            # 空正文也按量计费（推理 token 照算）：先记 token 再失败，否则恰好在要排查的
            # 场景里报表少算（陷阱 26）。它不算一次成功调用，只进 token
            _record_tokens(purpose, messages, "", getattr(resp, "usage", None))
            raise EmptyCompletionError(
                f"服务商返回了空正文（finish_reason={finish}，max_tokens={max_tokens}）",
                retryable=finish != "length",
            )
        if finish == "length":
            # 推理模型把推理 token 也算进 max_tokens，上限过低会先表现为这条，再严重就是上面的空正文
            logger.warning("LLM 输出触顶 max_tokens=%s（%s），正文可能被截断", max_tokens, purpose)
        return text, getattr(resp, "usage", None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM 调用失败，%s：%s", "将重试" if _should_retry(exc) else "不重试", exc)
        raise


def _token_counts(messages: list[dict], text: str, usage: object) -> tuple[int, int, bool]:
    """(prompt, completion, 是否估算)。服务商没给 usage（或结构不对）时按 estimate_tokens 估。"""
    prompt = token_count(getattr(usage, "prompt_tokens", None))
    completion = token_count(getattr(usage, "completion_tokens", None))
    if prompt is None or completion is None:
        prompt = sum(estimate_tokens(str(m.get("content") or "")) for m in messages)
        return prompt, estimate_tokens(text), True
    return prompt, completion, False


def _record_tokens(purpose: str, messages: list[dict], text: str, usage: object) -> None:
    """只记 token、不记调用次数：给拿到了响应却仍判失败的那次尝试用。"""
    meter = current_meter()
    if meter is None:
        return
    try:
        prompt, completion, estimated = _token_counts(messages, text, usage)
        meter.record_tokens(
            purpose, prompt_tokens=prompt, completion_tokens=completion, estimated=estimated
        )
    except Exception:  # noqa: BLE001
        logger.warning("LLM 调用计数失败（不影响调用结果）", exc_info=True)


def _record_call(
    purpose: str, messages: list[dict], text: str, usage: object, seconds: float
) -> None:
    """记一次成功调用。服务商没给 usage（或结构不对）时按 estimate_tokens 估，并标明估算。"""
    meter = current_meter()
    if meter is None:
        return
    try:
        prompt, completion, estimated = _token_counts(messages, text, usage)
        meter.record_call(
            purpose,
            prompt_tokens=prompt,
            completion_tokens=completion,
            estimated=estimated,
            seconds=seconds,
        )
    except Exception:  # noqa: BLE001
        logger.warning("LLM 调用计数失败（不影响调用结果）", exc_info=True)


async def chat(
    messages: list[dict],
    *,
    temperature: float = 0.7,
    model: str | None = None,
    max_tokens: int | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    purpose: LLMPurpose | str | None = None,
) -> str:
    """发起一次对话补全，返回纯文本内容。带重试。

    base_url / api_key 留空即用全局配置；传入时可把某类调用（如 selector
    打分）路由到另一个服务商或本地模型，同时保持本模块为唯一出口。
    `purpose` 只用于计数归类（工单25），漏标的记为 untagged。
    """
    key = purpose_key(purpose)
    started = time.monotonic()
    try:
        text, usage = await _complete(
            messages,
            temperature=temperature,
            model=model,
            max_tokens=max_tokens,
            base_url=base_url,
            api_key=api_key,
            purpose=key,
        )
    except Exception:
        meter = current_meter()
        if meter is not None:
            meter.record_failure(key, seconds=time.monotonic() - started)
        raise
    _record_call(key, messages, text, usage, time.monotonic() - started)
    return text


async def chat_safe(
    messages: list[dict],
    *,
    temperature: float = 0.7,
    model: str | None = None,
    max_tokens: int | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    purpose: LLMPurpose | str | None = None,
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
            purpose=purpose,
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
