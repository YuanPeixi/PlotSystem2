"""初始化 SQLite 索引数据库。

用法：
    python -m backend.utils.init_db
"""

from __future__ import annotations

import asyncio

from backend.config import settings
from backend.utils.db import db_path, init_db
from backend.utils.logger import get_logger

logger = get_logger("init_db")


async def _main() -> None:
    settings.ensure_dirs()
    await init_db()
    logger.info("数据库初始化完成：%s", db_path())


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
