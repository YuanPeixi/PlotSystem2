# 工单13：场景决策接口缺少幂等保护 + "下一场"可编辑范围过窄

**优先级**：P1
**预估改动范围**：中（1 个核心文件 + schema 扩展 + 简单前端表单）
**依赖**：建议先了解工单01（回滚分支"创建新场景"的写法）和工单10（`_active_scenes`
的幂等保护模式，本工单是同一类问题在另一个入口的体现）

---

## 1. 背景（问题 a：幂等性缺失）

工单10 修复了 `POST /scenes/{id}/start` 重复点击导致同一场景被并发运行两次的问题，
但防护只加在了 `orchestrator.run_scene()` 内部（用 `_active_scenes` 集合守卫）。

**`POST /scenes/{id}/decision`（`apply_decision`）完全没有类似的守卫**。当决策类型是
`next_scene` 或 `rollback` 时，`apply_decision` 每次调用都会创建一个**全新的 `Scene`
对象（`new_id()`）**：

```python
# backend/services/orchestrator.py, apply_decision
elif decision.decision_type == DecisionType.NEXT_SCENE.value:
    ...
    new_scene = await create_scene_from_config(scene.project_id, scene.branch_id, config)
    new_scene.parent_scene_id = scene.scene_id
    await repository.save_scene(new_scene)
    decision.next_scene_id = new_scene.scene_id
```

如果用户在前端快速连续点击两次"下一场"（或网络重试/双标签页），两个几乎同时到达的
`POST /scenes/{id}/decision` 请求会各自：
1. 各自调用一次 `director.make_decision`（多消耗一次 LLM 调用）；
2. 各自调用 `plan_scene` 规划（又一次 LLM 调用）；
3. 各自 `create_scene_from_config` 创建**两个不同的新 `Scene`**，且都把
   `parent_scene_id` 设为同一个原场景 `scene_id`。

结果：同一个 `branch_id` 下出现了两个"同父"的场景，**没有任何正式的 `Branch` 记录
被创建**，但效果上等价于隐性分叉了剧情树（该分支下场景列表变成了非线性的两个分支），
且用户大概率只会点开其中一个继续玩，另一个变成静默产生的孤儿数据、还额外消耗了
2 次 LLM 调用的 token。`rollback` 分支存在完全相同的问题（`_apply_character_states`
+ 创建新场景，同样没有幂等保护）。

## 2. 背景（问题 b：下一场可编辑范围过窄）

`DecisionRequest`（`backend/api/schemas.py`）目前只有：

```python
class DecisionRequest(BaseModel):
    decision_type: str
    extra_turns: int | None = None
    next_scene_description: str | None = None   # <-- 唯一能影响"下一场"内容的字段
    rollback_snapshot_id: str | None = None
    new_initial_conditions: dict | None = None   # <-- 仅 rollback 使用
    rollback_notes: str | None = None
```

`next_scene` 分支里，用户能改的只有一段自然语言描述（`next_scene_description`），
其余全部交给 `plan_scene` → `DirectorAgent.plan_scene` 自动决定：
`participating_characters`（参与角色）、`location`（地点）、`initial_conditions`
（初始条件/环境变量）。按最初设想，这些应该是用户（导演）在"下一场"前**可见、
可修改**的内容，而不是纯粹交给 AI 黑盒决定——尤其是 `initial_conditions` 一旦接入
工单07的 `WorldState`，这里就是用户手动调整"世界变量"的自然入口。

## 3. 目标（Definition of Done）

### 3.1 幂等保护（核心，优先做）

1. 参考工单10 的模式，在 `apply_decision` 内部对 `next_scene`/`rollback` 两个分支
   增加基于 `scene_id` 的守卫（可复用工单10 的 `_active_scenes` 集合，或新增一个
   独立的 `_deciding_scenes: set[str]` 避免与"场景运行中"语义混淆）：
   ```python
   if scene_id in _deciding_scenes:
       logger.warning("场景 %s 的决策正在处理中，忽略重复提交", scene_id)
       raise ConflictError(...)  # 或返回一个明确的"处理中"响应，由前端提示用户
   _deciding_scenes.add(scene_id)
   try:
       ...原有逻辑...
   finally:
       _deciding_scenes.discard(scene_id)
   ```
   注意检查与写入之间同样不能有 `await`。
2. `backend/api/scenes.py`（或 `director.py`，视 `/decision` 路由实际所在文件）的
   决策接口捕获该冲突并返回明确的 4xx 响应，前端据此提示"决策正在处理中，请勿重复
   提交"而不是静默失败或重复跳转。
3. 补充并发测试（参考 `tests/test_orchestrator.py` 里工单10 新增的并发测试写法）：
   并发调用两次 `apply_decision(scene_id, ...)`，断言只产生一个新 `Scene`。

### 3.2 扩展"下一场"可编辑范围

1. 扩展 `DecisionRequest`，新增可选字段（均为 `None`/空时保持现有"AI 自动决定"行为，
   不破坏现有调用方）：
   ```python
   next_participating_characters: list[str] | None = None
   next_location: str | None = None
   next_initial_conditions: dict | None = None
   ```
2. `apply_decision` 的 `next_scene` 分支：调用 `plan_scene` 得到 AI 建议的 `SceneConfig`
   后，若上述字段非空则覆盖对应值，再传给 `create_scene_from_config`（即"AI 先给一版
   建议，用户可在提交前覆盖"，具体到前端可以是"生成建议 → 展示可编辑表单 → 确认提交"
   两步交互，也可以先做成"一次性提交所有覆盖字段"的简化版，视前端排期决定）。
3. 前端 `DirectorPanel.vue`（下一场表单）在原有的"叙事目标"文本框基础上，增加：
   角色多选（复用 `charactersStore` 现有的角色列表）、地点文本框、
   初始条件/环境变量的简单 key-value 编辑器（先做最小可用版本，不需要复杂 UI）。
4. 与工单07（`WorldState`）联动：若07已完成，`next_initial_conditions` 里对
   `WorldState.variables` 的覆盖应该走统一的写入接口，而不是只停留在
   `Scene.initial_conditions`（一次性、不跨场景传递）这个更窄的字段上——具体如何
   衔接可在07落地后再细化，本工单先保证"用户能在下一场前看到并编辑"这个交互闭环。

## 4. 涉及文件

- `backend/services/orchestrator.py`（核心：幂等守卫 + `next_scene` 分支支持覆盖字段）
- `backend/api/schemas.py`（`DecisionRequest` 扩展）
- `backend/api/scenes.py` / `backend/api/director.py`（决策路由的冲突响应处理）
- `frontend/src/components/DirectorPanel.vue`、`frontend/src/stores/director.ts`（下一场表单扩展）
- `tests/test_orchestrator.py`（新增并发决策测试 + 覆盖字段测试）

## 5. 验收方式

1. 并发（`asyncio.gather`）调用两次 `apply_decision(scene_id, next_scene_decision)`，
   断言只创建了一个新 `Scene`，第二次调用能观察到明确的"处理中"日志/异常。
2. 前端连续快速点击"下一场"按钮两次，确认只产生一个新场景，且按钮在请求处理期间
   应变为禁用状态（UI 层面的辅助防护，非唯一防线）。
3. 提交 `next_participating_characters`/`next_location`/`next_initial_conditions`
   非空的决策请求，确认新创建场景的对应字段确实是用户提交的值，而非 AI 自动规划的值。
4. 不提交上述覆盖字段时，行为与改动前一致（纯 AI 自动规划），确认不破坏现有默认路径。
