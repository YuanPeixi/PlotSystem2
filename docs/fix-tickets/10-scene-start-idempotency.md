# 工单10：修复重复点击"开始模拟"导致场景并发分叉运行

**优先级**：P1
**预估改动范围**：小（1 个核心文件为主，1 个辅助文件）
**依赖**：无（建议了解 01，回滚分支里 `asyncio.create_task(run_scene(...))` 的调用方式与本工单相关）

---

## 1. 背景

场景启动接口 `POST /scenes/{scene_id}/start` 通过 FastAPI `BackgroundTasks` 触发
`orchestrator.run_scene(scene_id)` 后台运行，前端 `Director.vue` 的"开始模拟"按钮虽然
用 `sceneStore.running` 做了 `:disabled` 绑定，但这只是 UI 层面的软限制：

- `running.value = true` 是在 `startSimulation()` 内部同步设置的，但 DOM 上按钮真正
  变为 `disabled` 要等 Vue 下一次渲染 flush；连续两次很快的点击（或调用方通过接口直接
  发两次请求，例如网络重试、脚本、另一个浏览器标签页）完全可能在按钮变灰之前就已经
  各自发出了一次 `start` 请求。
- 后端 `start_scene` 接口和 `orchestrator.run_scene` 对"同一个 scene_id 是否已在运行"
  **没有任何检查**，两次请求会各自 `background.add_task` 一次，产生两个完全独立的
  `SceneEngine` 并发运行。

## 2. 精确错误位置

文件：`backend/api/scenes.py`：

```python
@scene_router.post("/{scene_id}/start")
async def start_scene(scene_id: str, background: BackgroundTasks) -> ApiResponse:
    """开始模拟（后台运行，进度经 SSE 推送）。"""
    await repository.get_scene(scene_id)  # 校验存在
    background.add_task(orchestrator.run_scene, scene_id)   # <--- 没有检查是否已在运行
    return ApiResponse.ok({"status": "started"})
```

文件：`backend/services/orchestrator.py`，函数 `run_scene`（约第 239-263 行）：

```python
async def run_scene(scene_id: str) -> None:
    scene = await repository.get_scene(scene_id)
    agents = await build_character_agents(scene.project_id, scene.participating_characters)
    ...
    engine = SceneEngine(scene, config, agents, sm)
    if scene.dialogue_log:
        engine.inject_history(scene.dialogue_log)
    _running_engines[scene_id] = engine   # <--- 写入前没有检查 key 是否已存在
    ...
```

`_running_engines` 这个全局字典**只被 `pause_scene` 用来查找引擎**，从未在 `run_scene`
开头用来判断"该场景是否已在运行"。两次几乎同时到达的调用会各自：

1. `await repository.get_scene(scene_id)` 读到同一份场景快照；
2. `await build_character_agents(...)` 各自构建一套独立的 `CharacterAgent` 列表；
3. 后到达的一次直接覆盖 `_running_engines[scene_id]`；
4. 两个 `engine.run()` 并发跑，都会：
   - 往同一个 SSE 频道 `events.publish(scene_id, "turn", ...)` 推送，前端对话日志出现
     交错/重复的两条并行剧情（即"分叉运行"现象）；
   - 结束时都调用 `_persist_character_states` + `repository.save_scene(scene)`，
     后完成的覆盖先完成的，导致角色状态/对话日志不一致、可能"丢线"；
   - 都各自触发一次 `DirectorAgent.evaluate_scene` + `repository.save_evaluation`，
     产生重复的评估记录。

## 3. 目标（Definition of Done）

1. 在 `backend/services/orchestrator.py` 中新增一个模块级 `set[str]`（如 `_active_scenes`），
   在 `run_scene` **函数最开头、第一个 `await` 之前**做"检查并标记"：
   ```python
   if scene_id in _active_scenes:
       logger.warning("场景 %s 已在运行中，忽略重复启动请求", scene_id)
       return
   _active_scenes.add(scene_id)
   ```
   这两行之间不能插入任何 `await`，依赖单线程事件循环保证其原子性，从根本上防止并发
   任务同时通过检查。
2. 在 `run_scene` 的 `finally` 块中（与现有 `_running_engines.pop(scene_id, None)` 一起）
   补充 `_active_scenes.discard(scene_id)`，确保场景运行结束/异常后能重新启动。
3. 新增辅助函数 `is_scene_active(scene_id: str) -> bool`，供 API 层查询。
4. 在 `backend/api/scenes.py` 的 `start_scene` 接口中调用 `is_scene_active` 做一次前置检查，
   若已在运行则直接返回 `{"status": "already_running"}`（不再 `add_task`），
   给正常场景下的重复点击一个更及时的响应（注意：这只是锦上添花，
   真正的并发安全保证来自第 1 条里 `run_scene` 内部的原子检查）。
5. `apply_decision` 的 `CONTINUE` 分支（`asyncio.create_task(run_scene(scene_id))`）
   无需改动，会自然复用同一层保护。

## 4. 涉及文件

- `backend/services/orchestrator.py`（核心改动：`_active_scenes` 集合 + `run_scene` 守卫 + `is_scene_active`）
- `backend/api/scenes.py`（`start_scene` 接口调用 `is_scene_active` 前置检查）
- 测试文件：`tests/test_orchestrator.py` 新增并发启动测试

## 5. 验收方式

1. 单元测试：并发（或连续）调用两次 `orchestrator.run_scene(scene_id)`（可用
   `asyncio.gather` 模拟），断言只有一次真正执行了 `SceneEngine.run()`
   （例如通过 mock `SceneEngine.run` 计数调用次数，期望为 1）。
2. 手动流程：在场景详情页快速连续点击两次"开始模拟"，确认：
   - SSE 日志里只出现一条连贯的对话线，没有交错重复的轮次；
   - 场景结束后 `GET /scenes/{id}/log` 的对话记录数量与"只跑一次"时一致；
   - 后端日志里能看到一条"已在运行中，忽略重复启动请求"的告警。
3. `python -m pytest tests/test_orchestrator.py -q` 通过。
