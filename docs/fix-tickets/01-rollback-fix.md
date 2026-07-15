# 工单01：修复导演回滚（rollback）决策断点

**优先级**：P0
**预估改动范围**：中（1 个核心文件为主，2 个辅助文件）
**依赖**：无
**状态**：✅ 已修复（分支 `fix/rollback-build-status`，commit `3fd0d1f`，见 `backend/services/orchestrator.py` 的 `apply_decision`/`_apply_character_states` 及 `tests/test_orchestrator.py`）

---

## 1. 背景

PlotSystem 的导演决策有三种类型：`continue`（继续）/ `next_scene`（下一场）/ `rollback`（回滚）。
`continue` 和 `next_scene` 分支都会在处理完后设置 `decision.next_scene_id`，前端
（`frontend/src/stores/scenes.ts` 的 `submitDecision`）检测到这个字段后会自动
`joinScene(nextId)` 刷新对话日志区域。

**但 `rollback` 分支目前处理不完整**，导致用户点击"回滚"按钮后界面看起来毫无反应。

## 2. 精确错误位置

文件：`backend/services/orchestrator.py`，函数 `apply_decision`（约第 244-290 行）：

```python
async def apply_decision(
    scene_id: str, human_override: DirectorDecision | None
) -> DirectorDecision:
    scene = await repository.get_scene(scene_id)
    evaluation = await repository.get_evaluation(scene_id)
    if evaluation is None:
        evaluation = SceneEvaluation(scene_id=scene_id)
    director = DirectorAgent(
        scene.project_id, GraphManager(scene.project_id), SnapshotManager(scene.project_id)
    )
    decision = await director.make_decision(evaluation, human_override)

    if decision.decision_type == DecisionType.ROLLBACK.value:
        # 回滚：恢复到模拟前快照
        target = decision.rollback_to_snapshot_id or scene.snapshot_id_before
        if target:
            sm = SnapshotManager(scene.project_id)
            await sm.restore_snapshot(target)          # <--- 问题1：返回值被丢弃，没有写回角色卡
                                                          # <--- 问题2：没有创建新场景
                                                          # <--- 问题3：没有设置 decision.next_scene_id
                                                          # <--- 问题4：new_initial_conditions 从未被使用

    elif decision.decision_type == DecisionType.CONTINUE.value:
        ...
```

### 具体问题清单

1. `sm.restore_snapshot(target)` 返回 `dict[str, CharacterState]`，但当前代码**直接丢弃返回值**，
   没有调用 `repository.save_character` 把恢复后的状态写回角色卡 JSON 文件。结果：图谱/记忆文件层面回滚了，
   但角色卡（情绪/目标/位置/关系）API 返回的仍是回滚前的旧值，前端显示不一致。
2. rollback 分支结束后**没有创建任何新场景**，也没有设置 `decision.next_scene_id`。
3. 前端 `frontend/src/stores/scenes.ts` 的 `submitDecision`：
   ```ts
   async function submitDecision(sceneId: string, payload: Record<string, unknown>) {
     const decision = await api.submitDecision(sceneId, payload)
     const nextId = (decision as Record<string, unknown>)?.next_scene_id as string | undefined
     if (nextId) {
       await joinScene(nextId)
     }
     return decision
   }
   ```
   因为 rollback 决策没有 `next_scene_id`，这里的 `if (nextId)` 永远不成立，界面不会有任何刷新动作，
   给用户"点了没反应"的直接体验。
4. `DecisionRequest.new_initial_conditions`（`backend/api/schemas.py` 第 73 行）→
   `DirectorDecision.new_initial_conditions`（`backend/models.py` 第 274 行）这条数据链路完整传递，
   但**全代码搜索确认从未被读取使用**——用户在回滚弹窗里填的"新初始条件"实际上被静默忽略。

## 3. 目标（Definition of Done）

修改 `apply_decision` 的 rollback 分支，使其行为完整闭环：

1. 调用 `sm.restore_snapshot(target)` 后，遍历返回的 `character_states`，
   逐个更新对应角色卡（至少同步 `current_emotion`/`current_goal`/`current_location`/`relationships`）
   并调用 `repository.save_character` 持久化。
2. 用 `decision.new_initial_conditions`（若为空则用原场景的 `initial_conditions`）和原场景的
   `participating_characters`/`location`/`branch_id` 创建一个新的 `Scene`（状态为 `PENDING`），
   可复用 `create_scene_from_config` 或直接构造 `Scene` 对象 + `repository.save_scene`。
   新场景的 `name` 建议加后缀标注"（回滚重演）"，`parent_scene_id` 设为原 `scene_id`。
3. 将新场景的 `scene_id` 赋值给 `decision.next_scene_id`。
4. 确认前端 `submitDecision` 收到 `next_scene_id` 后能正常 `joinScene` 并自动开始新一轮模拟
   （检查是否需要额外调用 `POST /scenes/{id}/start`，目前 `joinScene` 内部会调 `startSimulation` → `api.startScene`，
   应该是够用的，但要联调确认）。
5. 补充/修改单元测试：`tests/test_snapshot_manager.py` 或新建 `tests/test_orchestrator.py`，
   验证 rollback 后角色卡确实被更新、新场景确实被创建且 `initial_conditions` 与传入的 `new_initial_conditions` 一致。

## 4. 涉及文件

- `backend/services/orchestrator.py`（核心改动，`apply_decision` 函数）
- `backend/services/repository.py`（可能需要新增一个批量保存/读取辅助函数，非必须）
- `frontend/src/stores/scenes.ts`（确认联调无需改动，若有问题再改）
- 测试文件：新建或修改 `tests/test_orchestrator.py`

## 5. 参考：当前 CONTINUE / NEXT_SCENE 分支的正确写法（可作为模板）

```python
elif decision.decision_type == DecisionType.CONTINUE.value:
    extra = decision.extra_turns or 6
    scene.max_turns = scene.turns_completed + extra
    scene.status = SceneStatus.PENDING.value
    await repository.save_scene(scene)
    import asyncio
    asyncio.create_task(run_scene(scene_id))
    decision.next_scene_id = scene_id

elif decision.decision_type == DecisionType.NEXT_SCENE.value:
    next_desc = getattr(human_override, "next_scene_description", None) if human_override else None
    goal = next_desc or f"延续上一场（{scene.name}）的剧情走向"
    config = await plan_scene(scene.project_id, scene.branch_id, goal)
    new_scene = await create_scene_from_config(scene.project_id, scene.branch_id, config)
    new_scene.parent_scene_id = scene.scene_id
    await repository.save_scene(new_scene)
    decision.next_scene_id = new_scene.scene_id
```

rollback 分支应遵循同样的"创建新场景 + 设置 next_scene_id"模式，只是新场景的角色/地点/条件来自
快照恢复结果 + `new_initial_conditions`，而不是让导演重新规划。

## 6. 验收方式

1. 手动流程：跑通一个场景 → 触发评估 → 在前端导演面板点击"回滚"并填入新初始条件 JSON → 确认：
   - 界面自动跳转/刷新到新创建的回滚场景并开始新一轮模拟；
   - 该场景的 `initial_conditions` 确实包含你填入的内容（可通过 `GET /scenes/{id}` 检查）；
   - 参与角色的情绪/目标等状态确实是快照恢复后的值（非回滚前的最新值）。
2. `python -m pytest tests/test_orchestrator.py -q` 通过（新建的测试）。
