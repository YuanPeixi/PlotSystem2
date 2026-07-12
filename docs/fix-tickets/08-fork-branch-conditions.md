# 工单08：fork_branch / new_initial_conditions 生效

**优先级**：P2
**预估改动范围**：小-中（1-2个文件）
**依赖**：建议在工单01完成后做（复用其中"由决策创建新场景"的模式）

---

## 1. 背景

用户可以从某个快照 fork 出一个新分支，并指定"新条件"（`fork_conditions`），但当前实现
**只创建了 `Branch` 记录本身，没有基于 `fork_conditions` 自动创建一个待开始的新场景**。
用户 fork 完分支后，还要手动重新触发"导演规划"，而规划逻辑也完全不会读取
`branch.fork_conditions`，导致用户设置的"新条件"实际上从未被使用，与工单01中
`rollback` 的 `new_initial_conditions` 被忽略是同一类问题的另一处体现。

## 2. 精确错误位置

文件：`backend/snapshot/snapshot_manager.py`，方法 `fork_branch`：
```python
async def fork_branch(
    self,
    from_snapshot_id: str,
    new_conditions: dict,
    branch_name: str,
    director_notes: str = "",
) -> Branch:
    snap = await self.get_snapshot(from_snapshot_id)
    if snap is None:
        raise SnapshotNotFoundError(f"快照不存在: {from_snapshot_id}")
    await self.restore_snapshot(from_snapshot_id)

    branch = Branch(
        branch_id=new_id(),
        project_id=self.project_id,
        parent_branch_id=snap.branch_id or None,
        fork_from_snapshot_id=from_snapshot_id,
        fork_conditions=new_conditions,   # <-- 问题：conditions 被存下来了，但后续没有任何地方读取它
        name=branch_name,
        director_notes=director_notes,
    )
    await self.save_branch(branch)
    return branch
    # ↑ 问题：函数到这里就结束了，没有创建任何场景。
    #   用户 fork 完分支后，前端 Director.vue 只是把 branchId 切到新分支，
    #   但这个新分支下没有任何场景，用户必须手动点击"让导演规划"重新来一次，
    #   而 plan_scene（backend/services/orchestrator.py）也完全不知道 fork_conditions 的存在。
```

调用方 `backend/api/branches.py`：
```python
@snapshot_router.post("/{snapshot_id}/fork")
async def fork_branch(snapshot_id: str, project_id: str, req: ForkBranchRequest) -> ApiResponse:
    sm = SnapshotManager(project_id)
    branch = await sm.fork_branch(
        snapshot_id, req.new_conditions, req.branch_name, req.director_notes
    )
    return ApiResponse.ok(to_dict(branch))
```
只是把 `Branch` 对象原样返回，前端 `frontend/src/stores/director.ts` 的 `fork` 方法
调用完之后仅 `await loadBranches(projectId)` 刷新分支树，没有任何进一步动作。

## 3. 目标（Definition of Done）

1. 修改 `SnapshotManager.fork_branch`，在创建并保存 `Branch` 之后：
   - 获取原快照对应场景（`snap.scene_id`）的完整信息（`repository.get_scene(snap.scene_id)`，
     注意 `SnapshotManager` 当前没有直接依赖 `repository` 模块，需要新增 import，检查是否有循环
     依赖风险——`repository.py` 不依赖 `snapshot_manager.py`，所以直接 import 是安全的）。
   - 基于原场景的 `participating_characters`/`location`/`name`，以及合并后的
     `initial_conditions = {**原场景.initial_conditions, **new_conditions}`（新条件覆盖原条件），
     创建一个新的 `Scene`（`branch_id` 设为新分支 ID，`status=PENDING`，`parent_scene_id` 设为
     原 `scene_id`，`name` 建议加后缀"（分支：{branch_name}）"）。
   - 调用 `repository.save_scene(new_scene)` 持久化。
   - 返回值调整为同时包含 `branch` 和新创建的 `scene`（可以让 `fork_branch` 返回一个
     `tuple[Branch, Scene]`，或者新建一个小的结果 dataclass，具体看代码风格喜好，
     但要确保 API 层能拿到新场景 ID 返回给前端）。
2. 修改 `backend/api/branches.py` 的 `fork_branch` 路由，返回结果中包含新场景 ID：
   ```python
   return ApiResponse.ok({"branch": to_dict(branch), "scene": to_dict(new_scene)})
   ```
3. 修改前端 `frontend/src/stores/director.ts` 的 `fork` 方法，拿到返回结果后，
   如果包含新场景信息，调用 `useSceneStore().joinScene(scene.scene_id)`（跨 store 调用，
   Pinia 支持在一个 store 方法内 `import` 并使用另一个 `useXxxStore()`），让用户 fork 完分支后
   直接就能看到/开始新场景的模拟，而不需要额外手动规划。
4. 若原场景本身还没有开始模拟过（`snapshot_id_before` 为空场景理论上不会被 fork，因为 fork 依赖
   已存在的快照，快照只在场景 `run()` 时创建），此路径通常是安全的，但仍需做好
   `snap.scene_id` 对应场景可能已被删除的容错（`repository.get_scene` 抛出
   `SceneNotFoundError` 时的处理，比如降级为使用 `snap.character_states` 里的角色 ID 列表）。

## 4. 涉及文件

- `backend/snapshot/snapshot_manager.py`（核心改动：`fork_branch` 方法）
- `backend/api/branches.py`（`fork_branch` 路由返回值调整）
- `frontend/src/stores/director.ts`（`fork` 方法，处理返回的新场景并自动 joinScene）
- `frontend/src/pages/Director.vue`（如需要，确认 fork 触发处的调用逻辑无需大改）

## 5. 验收方式

1. 跑一个场景生成快照后，调用 fork API（可以直接用现有前端 UI 或 curl），传入一组
   `new_conditions`（比如 `{"tension": "高"}`）。
2. 确认返回结果里包含一个新场景 ID，且该场景的 `initial_conditions` 中确实包含你传入的
   `new_conditions`（与原场景条件合并，新条件生效覆盖）。
3. 确认前端 fork 操作完成后，界面能直接展示/进入这个新场景（无需用户手动再次规划）。
