"""只读排查：统计各角色长期记忆集合里完全相同的正文条目（工单26 §3.3）。

用法：
    python -m scripts.check_memory_dupes [--top N] [--project PROJECT_ID]

本脚本**只读**：不写入、不 upsert、不删除、不修改任何集合元数据。
工单26 之后新写入的条目按正文内容寻址（`memory.long_term.memory_id`），天然幂等；
本脚本用于评估**改造之前**已经沉淀在库里的重复数据。

确认重复严重时，最干净的处理是直接删掉该项目的 `chroma_db/` 目录重跑一遍
——长期记忆不是唯一真相源，它可以从 `Scene.dialogue_log` 重建。**不要**写脚本
去逐条删：无法区分"重复写入"与"角色确实把同一句话说了两遍"（工单26 红线 R3）。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from backend.config import settings

try:  # 契约6：chromadb 是可选依赖，缺失时本脚本必须能正常退出
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    _CHROMA_AVAILABLE = True
except Exception:  # noqa: BLE001
    chromadb = None  # type: ignore[assignment]
    _CHROMA_AVAILABLE = False

_PAGE = 1000


def _count_documents(collection) -> Counter[str]:
    """分页读出集合全部正文并按内容计数（大集合上一次性 get 会爆内存）。"""
    counter: Counter[str] = Counter()
    offset = 0
    while True:
        page = collection.get(limit=_PAGE, offset=offset, include=["documents"])
        docs = list(page.get("documents") or [])
        if not docs:
            break
        counter.update(docs)
        offset += len(docs)
        if len(docs) < _PAGE:
            break
    return counter


def _scan_project(project_dir: Path, top: int) -> int:
    """扫描一个项目的 chroma_db，返回重复条目总数（不含每组第一条）。"""
    db_dir = project_dir / "chroma_db"
    if not db_dir.exists():
        return 0

    client = chromadb.PersistentClient(
        path=str(db_dir),
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    project_dupes = 0
    print(f"\n=== 项目 {project_dir.name} ===")
    for col_meta in client.list_collections():
        name = getattr(col_meta, "name", col_meta)
        # 不传 embedding_function：只读文档正文，不需要也不应触发 embedding 调用
        collection = client.get_collection(name)
        counter = _count_documents(collection)
        total = sum(counter.values())
        dupes = [(text, n) for text, n in counter.most_common() if n > 1]
        extra = sum(n - 1 for _, n in dupes)
        project_dupes += extra
        flag = "  <-- 有重复" if extra else ""
        print(f"[{name}] 共 {total} 条，重复多出 {extra} 条{flag}")
        for text, n in dupes[:top]:
            snippet = text.replace("\n", " ")[:80]
            print(f"    x{n}  {snippet}")
    return project_dupes


def main() -> int:
    parser = argparse.ArgumentParser(description="统计长期记忆里的重复条目（只读）")
    parser.add_argument("--top", type=int, default=20, help="每个集合最多展示多少条重复正文")
    parser.add_argument("--project", default="", help="只扫描指定 project_id")
    args = parser.parse_args()

    if not _CHROMA_AVAILABLE:
        print("chromadb 未安装，长期记忆当前走降级路径，没有可扫描的向量库。")
        return 0

    projects_root = settings.projects_dir
    if not projects_root.exists():
        print(f"未找到项目目录：{projects_root}")
        return 0

    dirs = sorted(p for p in projects_root.iterdir() if p.is_dir())
    if args.project:
        dirs = [p for p in dirs if p.name == args.project]
        if not dirs:
            print(f"未找到项目 {args.project}")
            return 1

    total_dupes = 0
    for project_dir in dirs:
        try:
            total_dupes += _scan_project(project_dir, args.top)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 项目 {project_dir.name} 扫描失败，跳过：{exc}")

    print(f"\n合计重复多出 {total_dupes} 条。")
    if total_dupes:
        print(
            "处理建议：确认重复确实来自重复写入后，删掉对应项目的 chroma_db/ 重跑；"
            "长期记忆可从 dialogue_log 重建，不是唯一真相源。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
