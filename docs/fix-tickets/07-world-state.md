# 工单07：新增动态世界变量（WorldState）模型与流程

> **已完成**：PR #20，合入 main `cc8a021`→`53d36c5`。
> 本文写于工单 08 / 14 / 26 之前，**下面 §3.3 与 §3.6 的实现建议已与现行架构冲突**，
> 实际落地走了三处偏离（不改 `restore_snapshot()`、合并不写回 `Scene.initial_conditions`、
> 后置快照需补写），理由与红线见 [NOTES.md#t07](./NOTES.md#t07)，那里还记了 review
> 查出的五个洞（并发覆盖、保留字被顶替、预算绕过、分叉窗口、键带换行）。
> **按本文原样再实现一遍会破坏契约**，请以 `CLAUDE.md` 与 NOTES 为准。
>
> ⚠️ **§5 第 1 条的真实 LLM 端到端（连跑两场）仍未执行**（需凭据），自动化用例用的是构造的 delta。
> §5 第 3 条要按分叉语义理解：rollback 会新建分支，应查询**新分支**的 world-state，
> 来源分支的世界变量本来就不会被改动。

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


## 附：自 CLAUDE.md 迁出的推理（2026-11-09）

> 规则本体（schema 1 兼容小节的陷阱 19）在 `.claude/rules/world-environment.md`。以下是被压缩掉的推演与案例。
> 适用范围：**仅 schema 1**（`Project.world_schema_version == 1`）。schema 2 用不可变世界版本取代了这套机制。

<a id="trap19-reasoning"></a>
### 陷阱 19 的推演（八条语义的来历）

**合并不写回 `Scene.initial_conditions`**。写回并落库会让分叉不变量 I5（`Scene₀.initial_conditions = {**来源场景条件, **C}`）
把此刻的世界快照当成**场景局部条件**永久带下去，从此这条分支上的同名世界变量再也改不动。

**保留字被顶掉**。世界变量垫在 name / location / description / opening_narration 之下、又摊在同一个 dict 里，同名就会顶掉本场设定：
场景设在王城、世界里存着 `location=首都`，角色与导演就双双读到"地点：首都"，一条跨场次沿用的默认值改掉了导演为这一场明确指定的地点。
三道拦截各自挡一个入口：`normalize_world_delta`（不让它落进 evaluations 表）、`merge_world_variables`（清掉库里已有的）、
`SceneEngine._reject_reserved`（构造参数谁都能传，最后一道且不进快照）。名单与 `CharacterAgent._BRIEF_KEYS` 必须同一常量：
漂移会出现"拦住了却不成句"或"没拦住又被顶掉"。

**预算两道闸门**。世界变量进的是**每一场、每个角色、每一轮**的 system prompt，比线索（`unresolved_threads`）更紧迫。
`merge_world_variables` 只拦得住导演写入这条路径，而 `world_state/{branch_id}.json` 摆在项目目录里、**明确支持人工编辑**：
手写一条五千字的变量、或塞进三百条，都会绕过写入侧直进每一轮的 prompt。所以读取侧
（`repository._deserialize_world_state` → `clamp_world_variables`）必须再压一次，并把**键和值都**塌成单行——
只塌值是做不全的：不变量说的是渲染出来的行，键值同在 `f"- {k}：{v}"` 一行里，三道闸门又都只按名字判保留字、不看形状，所以键的换行同样能凭空多出一条看似合法的世界变量。
**只压不写回**：读路径不该因为一次读取就改掉用户手编的文件，超限内容在下次合并落盘时自然收敛。
这几个纯函数拆到 `services/world_state.py` 的唯一理由是 import 方向：`repository → director_agent → inspection → repository` 成环。
静默丢弃世界事实比丢弃线索更难发现——它不落在任何列表里，只表现为下一场角色忽然不知道某件事了，所以淘汰必须 warning。

**delta 的 `None` = 删除**。没有它变量只增不减，迟早占满预算，之后每一条新的世界事实都被挤掉。空串按同义处理。

**后置快照要补写**。delta 出自评估，而后置快照在评估之前就打好了。不补写的话，从该快照分叉出的分支会缺掉本场对世界的改动，
而它的角色状态与导演历史都已包含本场。

**读-改-写的临界区必须从重读开始**。世界状态是整份文件覆盖写，而 `run_scene` 在开场读、评估之后才写，中间隔着整整一场 LLM。
`_active_scenes` 只挡得住同一个场景被启动两次，同一分支上的**两个不同场景**照样能并发跑完：A 写下"城池已沦陷"、
B 随后以空 delta 收尾，世界就只剩下开场那份"冬季"。只给写加锁救不了——锁到了也只是把过时副本安全地写进去，所以
`_apply_world_delta` 不接受 `run_scene` 那份副本，而在锁内自己重读。锁按 `(project_id, branch_id)` 分桶，工单18 起与分镜稿共用同一把。
**遗留边界**：B 整场是拿着开场的旧世界演的（看不见 A 中途写下的事实）。这是并发本身的语义，不是数据丢失；
要连它一起消掉得按分支串行整场推演，那会让第二次 `/start` 阻塞几分钟，属于产品决策，未做。（schema 2 用分支执行占用 `acquire_world_execution` 解决了这一点，代价是同分支第二个场景被拒。）

**补写窗口内拒绝分叉**。快照在 `create_snapshot` 把它写进 `snapshots` 表那一刻就对分叉可见（fork 只读快照，既不看 `scene.status`，
也不等 `run_scene` 往下走），而评估、`record_story_history`、`_apply_world_delta` 都要在那之后才跑完。`fork_from_snapshot` 只在分叉那一刻读一次
`snap.world_state_variables` 整份拷进新分支文件，之后的补写不会再传播——窗口内分叉成功则新分支**永久**缺失本场世界改动，且不像超预算淘汰有 warning 可查
（story_history 同理）。因此标记要**早于快照可见**挂上：引擎预生成后置快照 id、在 `create_snapshot` 之前经 `on_after_snapshot` 回调交给 orchestrator。
不要挪回 `run()` 返回之后（之间隔着 `create_snapshot` 提交事务后关连接等 await，窗口虽小但真实存在），也不要改用 `save_scene` 落库作为可见性起点
（可见性根本不由 scene 落库决定）。标记在评估+补写整个 try/finally 结束后才摘（评估失败意味着 delta 永远不会再补写，无限期挡着分叉才是真正的"漏掉本场世界变化"），
且 `finally` 必须罩住 `engine.run()` 本身：快照已建而引擎随后抛错时若不摘，这份快照就永久不可分叉。进程内状态、单进程假设。

**"进 system 不破契约 3"的前提**：世界变量开场即冻结、整场不变；工单11 §2.2 曾写的"更新后的环境状态在下一轮 `build_system_prompt` 中体现"属于场景内变量，
那样做会每轮击穿 prefix cache，落地时必须改走 user 块（即后来的【当前环境】）。

**注意（旧文档偏差，2026-11-09 核实）**：原 CLAUDE.md 的 4.1 表把 `Scene.environment_state` 写成"本场物件公开状态"且不区分 schema；
代码里该字段只在 schema 1 + adjudicate 档被写入，schema 2 不使用（`backend/models.py:761`）。
