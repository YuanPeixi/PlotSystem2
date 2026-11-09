---
paths:
  - "backend/api/**"
  - "backend/main.py"
  - "backend/exceptions.py"
  - "backend/services/events.py"
  - "frontend/src/api/**"
  - "frontend/src/stores/**"
---

# API 与 SSE 规则

本文件管：统一前缀与响应包络、异常到 HTTP 状态码的映射、接口表（查阅型）、SSE 事件类型与语义。
目录：前缀与包络 · 异常映射 · 接口表 · SSE 事件。
路径以 `backend/api/*.py` 为准（2026-11-09 已对照全部路由核实）。新增/修改接口要同步本表。
分层红线（契约 8）：`api/` 只做参数校验 → 调 `services` → `to_dict`，不写业务逻辑。
凡可被重试 / 重放触发的写接口都要设计幂等键（契约 5）。

## 前缀与包络（原 §8）

统一前缀 `/api/v1`（`main.py::API_PREFIX`，所有 router 在 `main.py` 里统一挂载）。响应包络：

```json
{ "success": true, "data": {}, "error": null, "timestamp": "..." }
```

## 异常映射（`main.py` 全局处理器）

| 异常 | 状态码 |
|------|------|
| `ConflictError` | 409 |
| `InvalidRequestError`（业务层校验失败，如分镜稿超预算） | 422 |
| `MemoryError`（`exceptions.MemoryError`，长期记忆继承失败） | 500 |
| 其余 `PlotSystemError`（含各类 NotFound、`LLMError`） | 404 |
| 未捕获的 `Exception` | 500 |

新增业务异常要在 `exceptions.py` 里继承 `PlotSystemError`，并确认它落在预期的状态码上。

## 接口表

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST / GET | `/projects` | 创建（可带 `narrative_goal` / `ending_criteria`）/ 列出项目 |
| GET / DELETE | `/projects/{project_id}` | 详情 / 删除（连带 `rmtree` 项目目录） |
| PATCH | `/projects/{project_id}` | 人工编辑 name/description/**主线目标**/结局标准（字段为 null 则不改） |
| POST | `/projects/{project_id}/seed` | 上传种子文本（multipart） |
| POST | `/projects/{project_id}/build` | 触发构建（后台任务） |
| GET | `/projects/{project_id}/build/status` | 构建进度 |
| GET | `/projects/{project_id}/graph` | 图谱可视化数据 |
| GET | `/projects/{project_id}/characters` | 角色列表 |
| GET / PATCH | `/projects/{project_id}/characters/{char_id}` | 角色详情 / 人工编辑 |
| GET | `/projects/{project_id}/characters/{char_id}/memory` | 运行时记忆（短期缓冲 + 事件摘要），读自快照 |
| GET | `/projects/{project_id}/characters/{char_id}/inspect` | 角色内部视图（人设 + 时点状态 + 三层记忆） |
| POST | `/projects/{project_id}/scenes/plan` | 导演规划，返回 SceneConfig（**不落库**）。请求体只有 `branch_id` 与可选 `scene_intent`；主线目标固定读 `project.narrative_goal` |
| POST | `/projects/{project_id}/scenes` | 创建场景。`objects_present` 选本场在场物件（每场上限 10，含不存在的物件 422） |
| GET | `/projects/{project_id}/scenes` | 列出场景（`?branch_id=` 可选） |
| GET | `/projects/{project_id}/scenes/{scene_id}` | 场景详情 |
| GET | `/scenes/{scene_id}` | 场景详情（无需 project_id） |
| POST | `/scenes/{scene_id}/start` | 开始模拟（后台任务）。已在跑→`already_running`；已完成→`already_completed`（不重跑） |
| POST | `/scenes/{scene_id}/pause` | 中断运行中的引擎 |
| GET | `/scenes/{scene_id}/log` | 完整对话日志 |
| GET | `/scenes/{scene_id}/stream` | **SSE** 实时流 |
| GET | `/scenes/{scene_id}/evaluation` | 导演评估 |
| GET / POST | `/scenes/{scene_id}/decision` | 查询已生效决策（幂等重放） / 提交决策 |
| GET | `/projects/{project_id}/autopilot` | 该项目最近一次自动推演会话（含已停止的与停止原因）；没有则 null（进程重启后也是 null） |
| POST | `/projects/{project_id}/autopilot` | 开启自动推演：`scene_id`、可选 `max_steps` / `max_consecutive_rollbacks`（缺省取 `.env`），`request_id` 必填（幂等键，同键重放返回同一会话）。已有进行中会话 409；越界、起点已有生效决策、起点已完成但评估不可用或已抵达结局 422。起点在跑→等它跑完；pending/paused→替用户开演；completed→立即决策 |
| DELETE | `/projects/{project_id}/autopilot` | 停止自动推演：不再往下接，正在演的这一场照常演完（要立刻中断另调 pause） |
| GET | `/projects/{project_id}/branches` | 分支树 |
| GET | `/projects/{project_id}/branches/{branch_id}/world-state` | 分支世界变量（只读）。无记录返回空变量而非 404。schema 2 项目返回完整世界版本 |
| POST | `/projects/{project_id}/branches/{branch_id}/world/commands` | schema 2：提交世界命令（`request_id` / `revision_id` / `operations` / `observers`） |
| POST | `/projects/{project_id}/branches/{branch_id}/world/adopt-definitions` | schema 2：采用项目物件规则（显式更新分支世界版本的物件定义） |
| GET | `/projects/{project_id}/branches/{branch_id}/world/events` | schema 2：最近已提交的世界事件（含分叉起点前的继承来源，不含来源分支之后的变化） |
| GET | `/projects/{project_id}/world/migration-preview` | schema 2：预览 schema 1 → 2 迁移（分类及内容指纹，不调 LLM） |
| POST | `/projects/{project_id}/world/migrate` | schema 2：执行迁移（显式选中的事实，事务内切换全项目所有分支）；运行/决策中的项目不能迁移 |
| GET | `/projects/{project_id}/branches/{branch_id}/storyboard` | 分支导演分镜稿。无记录返回空分镜稿而非 404；附 `goal_stale` 与这一刻的 `narrative_goal` / `current_goal_revision` |
| PUT | `/projects/{project_id}/branches/{branch_id}/storyboard` | 用户整份替换 `outline` / `memo`，带读取时的 `revision`：不匹配 409；超预算、引用不存在的 `beat_id` 422（不截断）；`request_id` 为幂等键，命中已生效的编辑视为重放（200、不写），同键不同内容 422；与当前内容完全相同视为无操作。`confirm_goal` 须同时带回读取时的 `goal_revision_seen`（缺失 422）。分支不存在 404 |
| GET | `/projects/{project_id}/objects` | 物件列表（含隐藏规则：面向用户/导演） |
| GET | `/projects/{project_id}/objects/{object_id}` | 物件详情 |
| POST | `/projects/{project_id}/objects` | 新建。`request_id` 必填，物件 ID 由它确定性生成，重放返回同一物件；超预算、`private` 无知情者或知情者不存在、名称/别名与其他物件重复（不区分大小写）422（不截断）；每项目上限 40 |
| PATCH | `/projects/{project_id}/objects/{object_id}` | 修改（字段为 null 不改），带读取时的 `revision`：不匹配 409；`request_id` 为幂等键；与当前内容相同视为无操作 |
| DELETE | `/projects/{project_id}/objects/{object_id}` | 删除，已不存在也成功（`existed` 区分） |
| GET | `/projects/{project_id}/snapshots` | 快照列表：id / scene_id / branch_id / label / created_at / `character_count`（SQL 侧投影，不带角色状态与导演历史） |
| POST | `/snapshots/{snapshot_id}/fork` | 从快照分叉（**需 `project_id` query 参数**）。新建分支 + 其上一个 pending 首场，**不自动开跑**；返回 `{branch, scene}`。目标快照所属场景的评估/补写仍在进行中返回 409，稍后重试 |
| DELETE | `/snapshots/{snapshot_id}` | 删除快照（**需 `project_id` query 参数**，按项目约束） |
| POST | `/projects/{project_id}/output` | 生成输出 |
| GET | `/output/{output_id}` | 取回生成结果 |

## SSE 事件类型与语义

事件：`turn`（新 DialogueTurn）、`turn_update`（已推过的轮次被服务端改写后的完整版本，带更大的
`revision`；目前只有环境裁决改动作状态时发）、`status`、`snapshot`、`evaluation`、`scene_error`、
`autopilot`（自动推演会话的最新状态，带 `current_scene_id`；推在刚收场那一场的流上，**先于**终态帧）。

- **自动 continue 时这一场的终态帧不推**，同一条流直接接着收到新一轮 `status: running` 与 `turn`。
- **业务失败事件必须叫 `scene_error` 而不是 `error`**：`error` 是 EventSource 原生连接错误事件名，
  同名会让前端把两者混在一个处理器里、丢掉 `message`。`fatal` 字段区分"整场挂了"与"仅自动评估失败"。
- 订阅建立时后端**先回放一帧 `status`**（带 `initial: true`）；首帧已是 `completed` / `paused`
  则服务端**直接结束流**，不留只会 ping 的连接。
- **逐轮落盘先于 SSE 推送**，所以铺底用的 GET 与随后的 `turn` 事件可能撞上同一轮：
  客户端必须按 `turn_id` 去重，并在终态时无条件以持久化日志为准。
- **`turn` 的去重语义不能用来送达改写**：同一 `turn_id` 再推一次会被丢掉，改写只能走 `turn_update`；
  客户端按 `revision` 比较，只替换更新的版本（前端 `utils/turns.ts::applyTurnUpdate`）。
- 事件总线是进程内内存（`services/events.py`），契约 9 单 worker 假设。

前端侧 SSE 消费（`stores/scenes.ts`）的约束见 `frontend.md` 第 5 节（SSE 与日志对账）：句柄必须捕获自己的连接、
`scene_error` 与 `error` 分两个处理器。
