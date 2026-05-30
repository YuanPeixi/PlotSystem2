"""SQLite 索引数据库访问层（aiosqlite）。

存储项目、场景、分支、快照的元数据索引。
完整的角色状态/记忆/图谱以文件树形式存储于各项目目录。
"""

from __future__ import annotations

import aiosqlite

from backend.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT DEFAULT '',
    status       TEXT DEFAULT 'initializing',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    data_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS branches (
    branch_id        TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL,
    parent_branch_id TEXT,
    name             TEXT DEFAULT '',
    created_at       TEXT NOT NULL,
    data_json        TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS scenes (
    scene_id        TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    branch_id       TEXT NOT NULL,
    parent_scene_id TEXT,
    name            TEXT DEFAULT '',
    status          TEXT DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    data_json       TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    scene_id     TEXT,
    branch_id    TEXT,
    label        TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    data_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    scene_id    TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    data_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outputs (
    output_id   TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    format      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    content     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scenes_project ON scenes(project_id);
CREATE INDEX IF NOT EXISTS idx_scenes_branch ON scenes(branch_id);
CREATE INDEX IF NOT EXISTS idx_branches_project ON branches(project_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_project ON snapshots(project_id);
"""


def db_path() -> str:
    """返回 SQLite 文件路径。"""
    settings.ensure_dirs()
    return str(settings.projects_db_path)


async def init_db() -> None:
    """创建所有表。"""
    async with aiosqlite.connect(db_path()) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


def connect() -> aiosqlite.Connection:
    """返回一个新的连接（调用方负责 async with 管理）。"""
    return aiosqlite.connect(db_path())
