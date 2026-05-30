"""场景模拟事件广播（用于 SSE）。

每个 scene_id 对应一个 asyncio.Queue 列表，run() 期间推送事件，
SSE 端点订阅队列。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

_subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)


def subscribe(scene_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers[scene_id].append(q)
    return q


def unsubscribe(scene_id: str, q: asyncio.Queue) -> None:
    if scene_id in _subscribers and q in _subscribers[scene_id]:
        _subscribers[scene_id].remove(q)
        if not _subscribers[scene_id]:
            del _subscribers[scene_id]


async def publish(scene_id: str, event_type: str, data: Any) -> None:
    """向某场景的所有订阅者推送事件。"""
    for q in list(_subscribers.get(scene_id, [])):
        await q.put({"event": event_type, "data": data})
