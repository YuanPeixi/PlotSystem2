# PlotSystem 修复工单索引

> 本目录下每份文档都是一个**独立、自包含**的修复工单，可以单独分发给任意 AI 编程助手 session 处理，
> 不依赖其他工单或历史对话上下文。每份工单包含：问题背景、精确错误位置、目标（Definition of Done）、
> 涉及文件、建议实现步骤、验收方式。
>
> 处理顺序建议按优先级（P0 → P3），但各工单之间除特别标注的依赖关系外，均可**并行**处理。

## 工单列表

| 编号 | 标题 | 优先级 | 依赖 | 状态 |
|---|---|---|---|---|
| [01](./01-rollback-fix.md) | 修复导演回滚（rollback）决策断点 | P0 | 无 | ✅ 已修复（分支 `fix/rollback-build-status`） |
| [02](./02-embedding-remote.md) | 长期记忆接入远程 Embedding（替代本地降级） | P0 | 无 | 待处理 |
| [03](./03-branch-switch-frontend.md) | 修复前端分支切换无联动问题 | P1 | 无 | 待处理 |
| [04](./04-director-context.md) | 补全导演评估上下文 + 实现 query_character_state | P1 | 无 | 待处理 |
| [05](./05-character-inspector.md) | 新增角色 Inspect（导演视角详情）前端入口 | P1 | 无（建议在 04 之后做，可复用 query_character_state） | 待处理 |
| [06](./06-dynamic-graph-writeback.md) | 场景结束后动态回写知识图谱 | P2 | 无 | 待处理 |
| [07](./07-world-state.md) | 新增动态世界变量（WorldState）模型与流程 | P2 | 建议了解 01（快照需纳入 WorldState） | 待处理 |
| [08](./08-fork-branch-conditions.md) | fork_branch / new_initial_conditions 生效 | P2 | 依赖 01（复用其"由决策创建新场景"的模式） | 待处理 |
| [09](./09-memory-quality-optional.md) | 记忆检索质量优化（分层加权 / 中文分词降级） | P3 | 建议在 02 完成后做 | 待处理 |
| [10](./10-scene-start-idempotency.md) | 修复重复点击"开始模拟"导致场景并发分叉运行 | P1 | 无 | 处理中 |
| [11](./11-selector-and-world-interaction.md) | 完善 Selector 发言模式 + 动作/环境交互处理 | P2 | 建议先做 07，可联动 06 | 待处理 |

## 项目背景速览（给不熟悉本项目的 session）

- 项目：`PlotSystem`，影视多智能体剧情推演系统。后端 FastAPI（`backend/`），前端 Vue3+Vite+Pinia（`frontend/`）。
- 唯一权威规范文档：仓库根目录 `CLAUDE.md`，任何架构改动需与其保持一致或同步更新。
- 核心链路：GraphRAG 建图 → 角色卡生成 → 场景引擎驱动多角色对话（AutoGen 可选） → 导演评估决策 → 快照/分支 → 输出。
- 数据存储：SQLite（`data/projects.db`，项目/场景/分支/评估元数据）+ 每项目 JSON 文件（角色卡）+ Kuzu 嵌入式图数据库 + ChromaDB 向量库。
- 启动：`python -m uvicorn backend.main:app --port 5001`（后端），`cd frontend && npm run dev`（前端，代理 `/api` → 5001）。
- 测试：`python -m pytest -q`（注意：全量跑可能因 chromadb→onnxruntime 触发 DLL 崩溃，可单独跑不涉及 chroma 的测试文件）。
