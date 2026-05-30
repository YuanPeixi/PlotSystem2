"""序列化工具：dataclass <-> dict/json，处理 datetime 与 Enum。"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar

T = TypeVar("T")


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def to_dict(obj: Any) -> Any:
    """将 dataclass（含嵌套）转为可 JSON 化的 dict。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _normalize(dataclasses.asdict(obj))
    return _normalize(obj)


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def to_json(obj: Any, *, indent: int | None = 2) -> str:
    """序列化为 JSON 字符串。"""
    return json.dumps(to_dict(obj), ensure_ascii=False, indent=indent, default=_default)


def from_json(text: str) -> Any:
    """反序列化 JSON 字符串。"""
    return json.loads(text)
