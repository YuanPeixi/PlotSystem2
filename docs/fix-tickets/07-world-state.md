# 工单07：新增动态世界变量（WorldState）模型与流程

**优先级**：P2
**预估改动范围**：中-大（新数据模型 + 多处接入点）
**依赖**：建议先了解工单01的内容（快照恢复逻辑需要同步纳入 WorldState，避免重复踩坑）

---

## 1. 背景

当前系统没有"世界状态"这个概念。`Scene.initial_conditions` 只是每个场景各自独立的一次性 dict，
场景之间不传递、不演变；`CharacterCard` 只有角色自己的状态，没有任何**项目级/分支级**的全局变量
（例如"当前季节""某势力当前态度""是否已发生某关键事件"）。这导致导演和角色都无法感知
"世界层面"随剧情推演产生的持续性变化。

## 2. 现状确认（无 bug，是"缺失的功能"）

- `backend/models.py` 中没有 `WorldState` 相关的 dataclass。
- `backend/snapshot/snapshot_manager.py` 的 `Snapshot` dataclass（`backend/models.py` 约第 280 行）
  字段为：`character_states` / `scene_context` / `graph_checkpoint` / `chroma_checkpoint`，
  **没有** `world_state` 字段。
- `backend/services/orchestrator.py` 的 `run_scene` 构建 `SceneConfig` 时，只使用
  `scene.initial_conditions`，没有任何"读取当前分支全局变量并合并"的步骤。

## 3. 目标（Definition of Done）

### 3.1 数据模型

在 `backend/models.py` 新增：
```python
@dataclass
class WorldState:
    """项目/分支维度的全局世界变量。"""
    project_id: str = ""
    branch_id: str = ""
    variables: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=now)
```
（`Any` 需要从 `typing` 导入。）

### 3.2 持久化

新建 `backend/services/world_state_repository.py`（或直接在 `repository.py` 中新增函数），实现：
- `async def get_world_state(project_id: str, branch_id: str) -> WorldState`
  （不存在则返回一个空 `WorldState`，不报错）
- `async def save_world_state(state: WorldState) -> None`

存储方式：简单起见，落地为 JSON 文件 `data/projects/{project_id}/world_state/{branch_id}.json`
（参考 `repository.py` 中 `_characters_dir` 的写法风格，新增一个 `_world_state_dir` 辅助函数）。
不需要现在就上图数据库或 SQLite。

### 3.3 场景开始前合并世界变量

修改 `backend/services/orchestrator.py` 的 `run_scene` 函数：在构建 `SceneConfig` 之前，
调用 `get_world_state(scene.project_id, scene.branch_id)`，将其 `variables` 合并进
`scene.initial_conditions`（注意：场景自己的 `initial_conditions` 中同名字段应优先于全局变量，
即"场景局部覆盖全局默认"的合并顺序）。这样 `CharacterAgent.build_system_prompt` 和
`DirectorAgent` 都能通过 `scene_context` 间接感知到全局变量（不需要改动 `CharacterAgent` 本身，
因为它已经是从 `scene_context`/`initial_conditions` 里读取信息的）。

### 3.4 场景评估后更新世界变量

修改 `backend/agents/director_agent.py` 的 `_EVAL_PROMPT`，新增一个可选输出字段
`world_state_delta`（JSON 对象，表示本场对世界变量的增量修改，允许为空对象 `{}`）：
```
  "world_state_delta": {"某势力态度": "敌对"}
```
`evaluate_scene` 方法解析这个字段（做好容错，缺失/格式错误时按空对象处理），把结果放进
`SceneEvaluation`（需要给 `SceneEvaluation` dataclass 新增一个 `world_state_delta: dict` 字段）。
在 `backend/services/orchestrator.py` 的 `run_scene` 中，评估完成后调用
`save_world_state`，把 `delta` 合并进当前 `WorldState.variables`（简单字典 `update()` 即可，
不需要深度合并）。

### 3.5 API 暴露（可选但建议）

新增一个只读路由 `GET /api/v1/projects/{project_id}/branches/{branch_id}/world-state`，方便前端
展示当前世界变量（前端展示部分本工单不强制要求实现 UI，但至少后端要能查询，方便调试和后续工单05的
Inspector 扩展使用）。

### 3.6 快照纳入 WorldState（重要，避免与工单01冲突）

修改 `backend/snapshot/models.py`（或 `backend/models.py` 中 `Snapshot` dataclass）新增
`world_state_variables: dict = field(default_factory=dict)` 字段。
修改 `backend/snapshot/snapshot_manager.py` 的 `create_snapshot`：调用时读取当前
`WorldState` 并存入这个字段；`restore_snapshot`：恢复时把这个字段的值写回
`WorldState`（调用 `save_world_state`）。这样"回滚"操作（工单01）才能保证世界变量也一并回滚，
不会出现"图谱/角色回滚了，但世界变量还是最新的"这种不一致状态。

## 4. 涉及文件

- `backend/models.py`（新增 `WorldState` dataclass，`SceneEvaluation` 新增字段，
  `Snapshot` 新增字段）
- `backend/services/repository.py` 或新建 `backend/services/world_state_repository.py`
- `backend/services/orchestrator.py`（`run_scene` 中读取合并 + 评估后更新）
- `backend/agents/director_agent.py`（`_EVAL_PROMPT` 新增字段 + `evaluate_scene` 解析逻辑）
- `backend/snapshot/snapshot_manager.py`（`create_snapshot`/`restore_snapshot` 纳入 world state）
- `backend/api/`（可选新增只读查询路由，建议放在 `backend/api/branches.py` 里）

## 5. 验收方式

1. 跑两场连续场景，第一场评估结果中人工验证/构造一个 `world_state_delta`（可以先用假数据测试
   合并逻辑，再用真实 LLM 输出测试端到端）。
2. 确认第二场场景开始时，`scene_context`（可以打日志确认 `CharacterAgent.build_system_prompt`
   实际收到的 `scene_context`）包含了第一场留下的全局变量。
3. 触发一次快照回滚（结合工单01），确认回滚后 `GET /projects/{id}/branches/{bid}/world-state`
   返回的变量值确实恢复到快照创建时的状态，而不是最新值。
