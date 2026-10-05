"""原子写文件的唯一实现（CLAUDE.md §10.1）。

临时名的两条硬约束都踩过坑：
- **唯一**：`path.with_suffix('.tmp')` 让 `meta.json` 与 `meta.tmp` 共用一个名字，并发补写互相覆盖；
- **不长于目标名**：叠加目标全名 + 32 位 uuid 净增 38 字符，目标名含 sha256 时越过 Windows
  MAX_PATH(260)，抛出伪装成 `FileNotFoundError` 的错误。截取目标名前缀 + 8 位 uuid 也不够：
  `crown.json` 这种手工起的短名会被拉长 9 个字符。

所以临时名完全不含目标名，长度按目标名量：`.{随机串}~`，随机串取 base36、长度 = 目标名 − 2。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

#: 临时名的随机串长度区间。下限保证唯一性（36^6 ≈ 22 亿），只有 ≤ 7 字符的目标名会因此
#: 超出目标长度 —— 这种名字离 MAX_PATH 最远；上限是没必要更长
_TOKEN_MIN = 6
_TOKEN_MAX = 16
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"  # Windows 文件名不区分大小写，不用大写
TEMP_PREFIX = "."
TEMP_SUFFIX = "~"


def temp_path_for(target: Path) -> Path:
    """同目录下一个唯一、且（目标名 ≥ 8 字符时）不长于目标名的临时路径。"""
    n = max(_TOKEN_MIN, min(_TOKEN_MAX, len(target.name) - len(TEMP_PREFIX) - len(TEMP_SUFFIX)))
    token = "".join(secrets.choice(_ALPHABET) for _ in range(n))
    return target.with_name(f"{TEMP_PREFIX}{token}{TEMP_SUFFIX}")


def is_temp_name(name: str) -> bool:
    return name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX)


def atomic_write_text(target: Path, payload: str) -> None:
    """原子替换目标文件：写临时文件 → fsync → replace，失败清理临时文件。"""
    tmp = temp_path_for(target)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
