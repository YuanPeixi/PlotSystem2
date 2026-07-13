"""pytest 公共夹具。

为避免污染真实数据目录，将 DATA_DIR 指向临时目录。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

TEST_FIXTURES_DIR = Path(__file__).parent / "test-fixtures"

# 在导入 backend 前设置临时数据目录
_TMP = tempfile.mkdtemp(prefix="plotsystem_test_")
os.environ["DATA_DIR"] = _TMP
os.environ.setdefault("LLM_API_KEY", "sk-test")


@pytest.fixture
def tmp_data_dir() -> Path:
    return Path(_TMP)


@pytest.fixture
def fixture_text():
    def _read(*parts: str) -> str:
        return TEST_FIXTURES_DIR.joinpath(*parts).read_text(encoding="utf-8")

    return _read


@pytest.fixture(autouse=True)
async def _init_db():
    """每个测试前确保数据库表存在。"""
    from backend.utils.db import init_db

    await init_db()
    yield
