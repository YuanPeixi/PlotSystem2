"""分支长期记忆初始化凭据。

collection 元数据只能描述已经存在的角色集合，无法表达“这个角色在分叉点尚无
collection”或“向量库不可用，因此新分支的正确起点就是空”。这里用分支级文件凭据
记录边界；文件放在 chroma_db 内，会随快照一起复制，供再次分叉时判断旧共享集合
是否仍可作为兼容来源。
"""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from uuid import uuid4


def _marker_path(db_dir: Path, branch_id: str) -> Path:
    digest = sha256(branch_id.encode("utf-8")).hexdigest()
    return Path(db_dir) / "branch_initialization" / f"{digest}.initialized"


def _pending_path(marker: Path) -> Path:
    """临时文件名必须比 marker 更短。

    marker 名已含 64 字符 sha256，若再叠加完整 name + 32 字符 uuid，全路径会比
    marker 长 38 字符 —— 项目名稍长就越过 Windows MAX_PATH(260)，write_text 抛出
    伪装成 FileNotFoundError 的错误，最终被升级成 MemoryError 让整个 fork 失败。
    """
    digest = marker.name.split(".", 1)[0]
    return marker.with_name(f".{digest[:16]}.{uuid4().hex[:8]}.tmp")


def is_fork_initialized(db_dir: Path, branch_id: str) -> bool:
    """该分支是否已有确定的长期记忆起点。"""
    return bool(branch_id) and _marker_path(db_dir, branch_id).is_file()


def mark_fork_initialized(db_dir: Path, branch_id: str) -> None:
    """原子记录分叉边界，包括没有任何 collection 的空起点。"""
    if not branch_id:
        raise ValueError("分叉初始化需要非空 branch_id")
    marker = _marker_path(db_dir, branch_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.exists():
        return
    pending = _pending_path(marker)
    try:
        pending.write_text("initialized\n", encoding="utf-8")
        os.replace(pending, marker)
    finally:
        pending.unlink(missing_ok=True)
