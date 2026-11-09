---
paths:
  - "backend/**/*.py"
  - "scripts/**/*.py"
  - "tests/**/*.py"
---

# Python 开发规范

本文件管：Python 代码的通用约定（原 §10.1）。目录：类型与 async · 数据类型分层 · 异常 ·
原子写文件 · 读人工可编辑 JSON · 时间戳 · 路径预算 · ruff。命名与提交规范见根 CLAUDE.md。

- 公共函数完整类型注解；文件头 `from __future__ import annotations`。
- 所有 IO（LLM / DB / 文件）必须 `async`；禁止 `time.sleep`。
- 内部数据用 `@dataclass`（集中在 `models.py`），跨 API 边界用 Pydantic（`api/schemas.py`）。
- 异常继承 `PlotSystemError`（`exceptions.py`），不要裸 `raise Exception`。
- `ruff` 通过（`pyproject.toml`：line-length 100，选 E/F/I/W/UP，已忽略 UP042）。

## 原子写文件

**唯一实现是 `utils/fileio.py`**（`atomic_write_text` / `temp_path_for`）：临时名不含目标名、
按目标名长度取随机串，replace 前 fsync，失败清理。**不要在别处手写一份。**
临时名必须唯一且不长于目标名——已踩过三次：
`f".{目标全名}.{uuid4().hex}.tmp"` 净增 38 字符，目标名含 sha256 时越过 Windows MAX_PATH(260)，
抛出伪装成 `FileNotFoundError` 的错误；`path.with_suffix('.tmp')` 让 `meta.json` 与 `meta.tmp`
共用一个名字，并发写互相覆盖；"目标名前 16 字 + 8 位 uuid"对短名又会拉长 9 字符。
测"不留临时文件"用 `is_temp_name`，不要写死某种 glob（命名一改断言就恒真）。

## 读人工可编辑的 JSON

损坏判定要含 `UnicodeDecodeError`：记事本另存为 ANSI 就是 GBK，它既不是 `OSError` 也不是
`JSONDecodeError`。

## 时间戳

反序列化时间戳统一走 `models.as_aware`：手写文件常不带时区，与系统写的带时区时间比较会抛 `TypeError`
（`_parse_created_at` / `_parse_snapshot_time` 已接上）。

## 路径预算

整个数据目录的路径预算约 65 字符，别再挥霍。默认布局下最长的两条已达 195（分支初始化 marker，
含 64 字符 sha256）与 205 字符（`snapshots/{id}/chroma_collections/{uuid}/data_level0.bin`，
**由 Chroma 生成、我们控制不了**）。新增路径层级前先量一量；`DATA_DIR` 建议放浅目录。
根治要么开注册表 `LongPathsEnabled`，要么全路径加 `\\?\` 前缀（要统一所有构造点，第三方库兼容性未验证）
——都是环境/架构决策，真踩到再上。
