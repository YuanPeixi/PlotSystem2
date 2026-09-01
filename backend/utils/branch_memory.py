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
    pending = marker.with_name(f".{marker.name}.{uuid4().hex}.tmp")
    try:
        pending.write_text("initialized\n", encoding="utf-8")
        os.replace(pending, marker)
    finally:
        pending.unlink(missing_ok=True)
