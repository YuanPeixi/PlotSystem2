# 工单03：导演工作台前端统一修复

**优先级**：P1
**范围**：分支、场景、快照导航与页面恢复；合并原工单19
**依赖**：工单13已完成；工单23为独立增强，不阻塞本工单恢复后端已有数据

---

## 1. 问题

导演页目前把导航身份和场景内容都留在临时 Pinia 状态中：点击分支只修改本地 `branchId`，
不会加载该分支的场景；`joinScene()` 又把查看、首次启动和重连混为一谈，可能在查看历史时调用
`POST /start`。刷新后 URL 无法恢复分支与场景，已完成场景的评估、已生效决策和项目快照也没有
形成统一工作流。

后端已经以 `Scene.branch_id`、`Scene.dialogue_log`、`evaluations`、`decisions` 和 `snapshots`
保存这些内容，但缺少按项目/分支列出场景的 API；前端也没有把这些后端真相源组织起来。

## 2. 契约与边界

1. 严格保持根目录 `CLAUDE.md` 第7节九条契约，尤其是 API 分层、`data_json` 真相源、
   决策幂等和单进程 SSE 假设。
2. URL query 只保存导航身份：`branch_id`、`scene_id`；页面内容每次从后端重新读取，
   不依赖 Pinia 临时内存恢复。
3. 场景行为按后端状态严格分流：
   - `pending`：仅用户明确点击开始时调用一次 `POST /start`；
   - `running`：加载已持久化日志并订阅 SSE，绝不再次调用 `/start`；
   - `completed`：只读加载完整日志、评估和已生效决策，不订阅、不启动；
   - 其他终态：只读展示后端已有内容。
4. 查看历史场景、自动选择场景、URL 恢复都不得隐式启动模拟。
5. SSE 断开或场景完成后重新获取完整 `Scene.dialogue_log` 对账。
6. 快照时间线是导航与 fork 入口，不代表恢复运行时状态；rollback 仍只能通过导演决策语义执行。
7. 工单23独立负责逐轮持久化、运行进程状态、重启对账等后端能力。本工单不实现这些内容；
   在23完成前，running 场景刷新只能恢复后端当时已经持久化的数据。
8. 不维护当前无调用价值的 `Branch.scenes` 冗余字段，不新增核心依赖，不做无关视觉重构。

## 3. 实现目标

### 3.1 分支与场景导航

- 后端新增 `GET /api/v1/projects/{project_id}/scenes?branch_id=...`，`branch_id` 可选，
  直接复用 `repository.list_scenes(project_id, branch_id)` 并返回场景列表。
- 前端 API 与 scene store 接入按分支查询，切换分支时清理旧选择并加载正确列表。
- `SceneTree` 接收当前 `branch_id`，为选中分支提供明确状态。
- 展示分支下的历史场景、状态、轮次等必要信息；自动选择 URL 指定场景或合理默认场景，
  但自动选择只查看，不启动。
- 处理空分支、加载中、请求失败、URL 分支/场景无效及场景已删除；无效身份应回退到有效选择并修正 URL。

### 3.2 场景查看与恢复

- 点击场景后重新请求场景详情，以 `Scene.dialogue_log` 覆盖或对账当前日志。
- 同步请求 evaluation；选中或恢复场景时请求 `GET /scenes/{scene_id}/decision`。
- 已生效决策要恢复 `decision_type`、`next_scene_id` 等结果并锁定重复提交入口；后端工单13的
  幂等与冲突契约不得改弱。
- `branch_id` 与 `scene_id` 使用 Vue Router query 保存；刷新、离开再返回时重新拉取分支树、
  场景列表和场景内容后恢复视图。
- 页面卸载或切换场景时关闭旧 SSE；running 场景仅订阅，pending 场景显示明确的开始操作。

### 3.3 快照导航

- 页面初始化时加载项目快照，补充正式 `Snapshot` TypeScript 类型。
- 展示快照时间线，至少标明所属场景、创建时间和 before/after 位置。
- 提供从快照 fork 的入口，复用既有 `POST /snapshots/{snapshot_id}/fork?project_id=...`；
  fork 成功后刷新分支树与快照，并导航到新分支。

### 3.4 状态与视觉

- 为分支列表、场景列表、场景详情、评估或决策、快照分别提供可理解的 loading、empty、error 状态。
- 保持现有暗色设计语言，优先提升导演工作流的信息层次、选择反馈和操作安全性。

## 4. 主要文件

- `backend/api/scenes.py`
- `frontend/src/api/client.ts`
- `frontend/src/types/index.ts`
- `frontend/src/stores/scenes.ts`
- `frontend/src/stores/director.ts`
- `frontend/src/pages/Director.vue`
- `frontend/src/components/SceneTree.vue`
- `frontend/src/components/DirectorPanel.vue`
- `tests/` 与前端测试目录
- `CLAUDE.md`、`docs/fix-tickets/README.md`

## 5. 验收

自动测试至少覆盖：

1. 后端按项目及可选分支正确查询场景。
2. 切换分支只展示该分支场景，并有明确选中状态。
3. 查看 completed 历史场景只读加载，不调用 `POST /start`。
4. 带 `branch_id`、`scene_id` 的 URL 刷新后恢复；无效 URL 有确定回退。
5. 已生效决策及 `next_scene_id` 恢复，前端不会提示重复提交。
6. 快照列表渲染 before/after 信息，fork 入口调用既有 API 并刷新导航。

人工验收还需检查 pending 的显式启动、running 的仅订阅重连、completed 的只读查看，以及
空分支、请求失败和场景被删除时页面不会停留在旧内容。

## 6. 完成记录

- 分支：`fix/director-workbench-navigation`
- 实现：按分支查询场景；URL 恢复分支/场景；pending 显式启动、running 仅订阅、completed
  只读；历史日志/评估/已生效决策恢复；快照时间线及 fork 入口；加载、空态与错误回退。
- 前端验证：`npm --prefix frontend test`，3 个测试文件、7 个用例通过；
  `npm --prefix frontend run build` 通过。
- 后端验证：`python -m pytest tests/test_scene_api.py -q`，1 个用例通过；
  `python -m ruff check backend/api/scenes.py tests/test_scene_api.py` 通过。
- 原工单19已并入，不再单独排期；索引与 `CLAUDE.md` 已同步。
- 保留边界：工单23尚未实现逐轮持久化、进程重启对账等能力。running 场景刷新时，本工单只能
  恢复后端当时已经持久化的 `dialogue_log`，不会擅自重跑模拟。
