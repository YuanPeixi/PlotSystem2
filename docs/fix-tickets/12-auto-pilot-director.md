# 工单12：新增 Auto Pilot（自动执行导演决策）

**优先级**：P2（新功能，非缺陷修复）
**预估改动范围**：中（后端编排 + 简单前端开关）
**依赖**：建议在工单10（决策接口幂等，见工单13）完成后做，避免自动循环触发并发问题

---

## 1. 背景

当前导演决策流程完全依赖人工：场景结束 → 自动评估（`DirectorAgent.evaluate_scene`）→
人工在前端点击"继续/下一场/回滚"三个按钮之一 → 提交 `POST /scenes/{id}/decision`。

`DirectorAgent.make_decision(evaluation, human_override)` 已经支持
`human_override=None` 的情况（此时完全采用 AI 自己的评估建议），说明**决策本身的
"自动模式"逻辑已经存在**，缺的是"谁来触发它、什么时候触发、触发几次后停下来"这一层
编排逻辑——目前没有任何代码路径会在没有人工点击的情况下调用 `apply_decision`。

## 2. 目标（Definition of Done）

### 2.1 触发机制

1. 在 `Project`（或 `Branch`，两者选一，建议先做 `Project` 级别的全局开关，更简单）
   新增字段 `auto_pilot: bool = False`。
2. 在 `backend/services/orchestrator.py` 的 `run_scene` 成功完成、评估
   （`director.evaluate_scene`）产出结果之后，新增判断：若该项目 `auto_pilot=True`，
   则**不等待人工**，直接调用 `apply_decision(scene_id, human_override=None)`，
   把返回的 `decision.next_scene_id` 对应场景继续投入运行（`rollback`/`next_scene`
   产生的新场景需要显式调用 `run_scene(new_scene_id)`；`continue` 分支本身已经在
   `apply_decision` 内部触发了 `run_scene`）。

### 2.2 安全阀（必须项，防止失控）

1. **最大自动连锁次数**：新增 `Project.auto_pilot_max_chain`（默认如 10），每次自动
   触发时计数，达到上限后自动关闭 `auto_pilot` 并推送一条 SSE `status` 事件告知前端
   "自动推演已达上限，请人工介入"，而不是无限跑下去（避免无限消耗 token/陷入剧情
   死循环）。
2. **rollback 特殊处理**：`rollback` 类型的自动决策存在"反复回滚同一个点"的风险
   （例如评估分数长期低于阈值，AI 每次都建议回滚到同一快照）。建议：连续 2 次
   自动回滚到*同一个* `snapshot_id` 时，强制关闭 auto_pilot 并转人工（记录日志说明
   原因），防止死循环回滚消耗资源却没有进展。
3. **随时可中断**：`auto_pilot=False` 的切换需要能在场景正在自动运行时立刻生效
   （下一次决策判断前检查最新的 `Project.auto_pilot` 值，而不是在启动时读一次就
   缓存），配合已有的 `pause_scene`/`SceneEngine.interrupt()` 可以让用户随时"踩刹车"。

### 2.3 前端

1. `Director.vue` 或 `Workspace.vue` 增加一个"Auto Pilot"开关（toggle），调用新增的
   `PATCH /projects/{id}` 或专门的 `POST /projects/{id}/auto-pilot` 接口。
2. 开启后，导演决策面板（`DirectorPanel.vue`）应显示"自动驾驶中"的状态提示，
   替代原本要求人工点击的三个按钮（但仍保留"立即暂停/接管"按钮，调用
   `pause_scene` + 关闭 `auto_pilot`）。
3. 达到最大连锁次数或触发反复回滚保护而被系统自动关闭时，前端需要通过 SSE
   收到明确提示，而不是静默切回人工模式。

## 3. 涉及文件

- `backend/models.py`（`Project` 新增 `auto_pilot`/`auto_pilot_max_chain`/
  内部连锁计数字段）
- `backend/services/orchestrator.py`（`run_scene` 结束评估后的自动触发逻辑、
  连锁计数与安全阀）
- `backend/api/projects.py`（新增/扩展开关接口）
- `frontend/src/components/DirectorPanel.vue`、`frontend/src/stores/director.ts`（开关与状态展示）
- 测试：新增 `tests/test_orchestrator.py` 用例，mock 评估结果验证连锁上限与反复回滚保护生效

## 4. 验收方式

1. 开启 auto_pilot，跑一个项目的场景，确认无需人工点击就能连续推进多场，
   且日志中每次自动决策都有明确记录（区分"人工"和"AI自动"两种来源）。
2. 人为构造一个评估结果始终建议 `rollback` 到同一快照的场景（可 mock
   `evaluate_scene` 返回值），确认触发 2 次后 `auto_pilot` 被自动关闭。
3. 构造一个连续 `continue`/`next_scene` 场景链，确认达到 `auto_pilot_max_chain`
   后自动停止并有前端可见提示。
4. 场景自动运行过程中手动关闭 `auto_pilot`，确认下一次决策判断前就能感知到关闭，
   不会再"多跑一场"才停下来。
