# 工单03：修复前端分支切换无联动问题

**优先级**：P1
**预估改动范围**：中（后端新增1个路由 + 前端3个文件）
**依赖**：无

---

## 1. 背景

后端 `Scene` 数据模型本身正确记录了 `branch_id` 字段（可通过 `repository.list_scenes(project_id, branch_id)`
正确过滤），但前端点击分支树切换分支后，**界面没有任何联动反应**，看起来像是"分支切换失效"。
根因是前端缺少"按分支加载场景"这一环节，且后端缺少对应的查询路由。

## 2. 精确错误位置

### 2.1 前端：点击分支只改了一个 ref，没有后续动作

文件：`frontend/src/pages/Director.vue`
```vue
<SceneTree :tree="directorStore.branchTree" @select="branchId = $event" />
```
```ts
const branchId = ref('')
onMounted(async () => {
  await charStore.load(props.projectId)
  await directorStore.loadBranches(props.projectId)
  branchId.value = directorStore.branchTree.roots[0]?.branch.branch_id || ''
})
```
`branchId` 变化后**没有任何 `watch`**，也没有调用任何"加载该分支场景"的逻辑。它目前唯一的用途是
"下次点击'让导演规划'时用哪个 branch_id 去规划新场景"（见 `plan()` 函数），与"查看这个分支已发生的剧情"
完全无关。

### 2.2 后端：没有"按分支列出场景"的路由

文件：`backend/api/scenes.py`，当前只有：
```python
@project_router.get("/{scene_id}")
async def get_scene(project_id: str, scene_id: str) -> ApiResponse:
    scene = await repository.get_scene(scene_id)
    return ApiResponse.ok(to_dict(scene))
```
**没有** `GET /projects/{project_id}/scenes?branch_id=xxx` 这种列表查询路由，尽管
`backend/services/repository.py` 里 `list_scenes(project_id, branch_id=None)` 函数早已实现
（第 234 行附近），只是从未被任何 API 路由暴露出来。

### 2.3 前端 API 客户端也缺少对应方法

文件：`frontend/src/api/client.ts`，`api` 对象中没有 `listScenes` 方法。

### 2.4 视觉反馈缺失

文件：`frontend/src/components/SceneTree.vue`：
```ts
h('div', { class: 'branch-pill', onClick: () => emit('select', node.branch.branch_id) }, ...)
```
分支节点没有"当前选中"的 class 绑定，用户点击后无法从 UI 上确认自己选中了哪个分支
（没有 prop 传入当前选中的 branchId，也没有对应的高亮样式）。

### 2.5（附带发现，可选一并修复）Branch.scenes 字段从未被写入

`backend/models.py` 中 `Branch.scenes: list[str] = field(default_factory=list)` 字段设计用于记录
该分支包含哪些场景 ID，但全代码搜索确认创建场景时（`backend/api/scenes.py` 的 `create_scene` 和
`backend/services/orchestrator.py` 的 `create_scene_from_config`）都没有把新场景 ID append 进对应
`Branch.scenes` 并调用 `sm.save_branch`。这个字段目前始终是空数组。这不是本工单的强制要求，
但如果顺手修，能让后续"分支内场景计数"等功能更容易实现。

## 3. 目标（Definition of Done）

1. **后端**新增路由：`GET /api/v1/projects/{project_id}/scenes?branch_id=xxx`（`branch_id` 可选，
   不传则返回该项目所有场景），调用现成的 `repository.list_scenes(project_id, branch_id)`，
   返回场景列表（建议只返回精简字段：`scene_id/name/status/turns_completed/branch_id/created_at`，
   避免把完整 `dialogue_log` 都传输，可以新建一个轻量序列化函数或直接用 `to_dict` 但注意体积）。
2. **前端 API 客户端**新增 `api.listScenes(projectId, branchId?)` 方法。
3. **前端 store**（`frontend/src/stores/scenes.ts`）新增一个方法，比如 `loadByBranch(projectId, branchId)`，
   调用上面的 API，把结果存入一个新的 `scenesInBranch` ref。
4. **`Director.vue`**：给 `branchId` 增加 `watch`，切换时调用 `sceneStore.loadByBranch(...)`。
   UI 上至少要展示"该分支下有哪些历史场景"的一个列表（哪怕只是简单的场景名+状态列表，点击可
   `joinScene` 查看该场景的对话日志），不能让切换后中间对话日志区域"毫无变化"。
   具体交互方式（自动加载最后一场 / 展示列表让用户选择）可自行设计，但必须要有明确的视觉变化。
5. **`SceneTree.vue`**：增加 `selectedBranchId` prop，当前选中节点加 `.active` class 并配一个
   明显的高亮样式（比如边框变色/背景变色），让用户能确认当前选中了哪个分支。
6. （可选，建议一并做）**创建场景时把 scene_id 写入 Branch.scenes**：
   在 `backend/api/scenes.py` 的 `create_scene` 和 `backend/services/orchestrator.py` 的
   `create_scene_from_config` 中，创建场景成功后读取对应 `Branch`（`SnapshotManager.list_branches`
   或新增一个 `get_branch(branch_id)` 辅助方法），把 `scene.scene_id` append 进 `branch.scenes`
   并调用 `sm.save_branch(branch)`。

## 4. 涉及文件

- `backend/api/scenes.py`（新增路由）
- `backend/services/repository.py`（确认 `list_scenes` 签名足够用，可能需要新增一个精简序列化函数）
- `frontend/src/api/client.ts`（新增 `listScenes`）
- `frontend/src/stores/scenes.ts`（新增 `loadByBranch` 和 `scenesInBranch` 状态）
- `frontend/src/pages/Director.vue`（新增 `watch(branchId, ...)` 和对应 UI 展示）
- `frontend/src/components/SceneTree.vue`（新增 `selectedBranchId` prop + 高亮样式）
- （可选）`backend/snapshot/snapshot_manager.py`（新增 `get_branch` 辅助方法，若尚不存在）

## 5. 验收方式

1. 创建至少两个分支（可通过 fork 功能或直接构造测试数据），每个分支下跑至少一个场景。
2. 前端进入导演页面，点击分支树中不同的分支节点：
   - 确认被点击的节点有明显高亮，能看出当前选中项；
   - 确认中间区域/新增列表区域展示了该分支下实际发生过的场景，且切换分支后内容确实变化；
   - 点击某个历史场景后能重新查看该场景的对话日志。
3. `GET /api/v1/projects/{id}/scenes?branch_id=xxx` 手动用浏览器或 curl 验证返回结果只包含该分支的场景。
