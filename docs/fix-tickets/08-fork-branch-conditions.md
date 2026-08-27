# 工单08：分叉（fork）语义收敛

**优先级**：P2
**预估改动范围**：中-大（阶段 A 约 6 个文件，阶段 B 约 5 个文件，建议拆两个 PR）
**依赖**：01 ✅、13 ✅、14 ✅、17 ✅
**契约影响**：契约 2、契约 4、契约 8（均须保持，不得破坏）

> **本工单不是"补两行让 `fork_conditions` 被读取"**。当前仓库里存在两套互相矛盾的分叉
> 实现，且其中一套依赖的隔离性根本不存在。本单的目标是先给"分叉"下一个可验证的定义，
> 再把两套实现收敛到这个定义上。

---

## 0. 严格定义（本工单的验收基准）

### 0.1 分叉原语

```
fork(S, C, name) -> (B', Scene₀)
```

| 记号 | 含义 |
|------|------|
| `S` | 来源快照（`Snapshot`），代表剧情时间轴上的一个**时点** |
| `C` | 覆盖条件（`dict`），用户对这条 IF 线的"如果……会怎样"的设定 |
| `B'` | 新建分支（`Branch`） |
| `Scene₀` | `B'` 上的首场场景，状态 `pending`，**不自动开跑** |

### 0.2 五条不变量

一次合法的分叉必须同时满足以下五条。**验收只认这五条，不认"功能跑通了"**：

| 编号 | 名称 | 表述 | 可验证方式 |
|------|------|------|-----------|
| **I1** | **起点一致** | `Scene₀` 开演时，每个参演角色的可观测状态（`current_emotion` / `current_goal` / `current_location` / `relationships` / 短期缓冲 / 事件摘要）**等于** `S` 中记录的状态，不多不少 | 跑 `Scene₀` 前调 `inspection.load_character_state(scene_id=Scene₀)`，逐字段比对 `S.character_states` |
| **I2** | **无副作用** | fork 不修改 `S` 所在分支的**任何**持久状态：不写角色卡、不动 Chroma、不动 Kuzu、不改任何既有 Scene | fork 前后对项目目录做文件清单/内容比对，只允许新增 |
| **I3** | **相互隔离** | `B'` 上产生的长期记忆 / 角色状态**不得**被 `B` 的后续场景读到，反之亦然 | 在两条分支上交替各跑一场，各自检索长期记忆，断言互不包含对方的台词 |
| **I4** | **可追溯** | `B'.parent_branch_id == S.branch_id` 且 `Scene₀.parent_scene_id == S.scene_id` | 直接断言字段 |
| **I5** | **条件生效** | `C` 中每个键都出现在 `Scene₀.initial_conditions` 中，且同名键**覆盖**从来源场景继承来的值 | 断言 `Scene₀.initial_conditions ⊇ C` |

### 0.3 由定义推出的三条结论

1. **`rollback` 是 fork 的特例**：`C = {}`、`name` 自动生成（"回滚重演"）。
   两者**必须**走同一个函数，否则 I3/I4 会在其中一条路径上悄悄退化 —— 这正是当前的状况。
2. **`continue` 不是 fork**：它在同一分支同一场景上加轮次续跑，不产生新时间线。
   本工单不碰它。
3. **fork 不得"恢复"任何东西**。I2 直接排除了 `SnapshotManager.restore_snapshot()` ——
   它 `rmtree` 后拷回旧副本，是就地覆盖，见 CLAUDE.md §4.2 陷阱 10 与 §12.2。
   起点一致（I1）只能靠**懒承接**（契约 4）达成：给 `Scene₀` 设
   `restore_snapshot_id = S.snapshot_id`，由 `run_scene` 时的
   `inspection.resolve_scene_states` 回填。

---

## 1. 现状对照

### 1.1 逐条不变量的差距

| | `fork_branch()`（`POST /snapshots/{id}/fork`） | `apply_decision` 的 rollback 分支 |
|---|---|---|
| **I1 起点一致** | ✗ 根本没有 `Scene₀` | ✓ `restore_snapshot_id` 懒承接 |
| **I2 无副作用** | ✓ 只 INSERT 一行 `branches` | ✓（PR #15 起改为 `get_snapshot` 只读） |
| **I3 相互隔离** | ✗ 见 1.2 | ✗ 见 1.2 |
| **I4 可追溯** | ◐ 有 `parent_branch_id`，但无 `Scene₀` | ✗ **新场景落在原 `branch_id` 下**，分支树看不出分叉 |
| **I5 条件生效** | ✗ `fork_conditions` 写入后全仓库零读取 | —（rollback 的 `C` 恒为空，不适用） |

**一句话**：叫 `fork` 的那个不产生分叉，产生分叉的那个不叫 fork。

### 1.2 I3 是共同的、且改不动的短板

CLAUDE.md §4.2 陷阱 10 的作用域表说明：四类状态里只有一类是分支隔离的。

| 状态 | 作用域 | 隔离 | 代码位置 |
|------|--------|------|---------|
| 快照里的 `CharacterState` | 时点级 | ✅ | `snapshot/snapshot_manager.py` |
| 角色卡 `current_*` | 项目级单值 | ❌ | `characters/{cid}.json` |
| Chroma 长期记忆 | 角色+项目级 | ❌ | `memory/long_term.py` 的 `collection_name = f"char_{cid}"` |
| Kuzu 图谱 | 项目级单文件 | ❌ | `knowledge_graph/graph_manager.py` |

因此**只改 `fork_branch()` 无法满足 I3**：两条分支交替推进，IF 线的角色会检索到主线
发生过的台词。做完也只是"看起来分叉了"。这就是本工单必须分两阶段的原因。

### 1.3 ⚠️ 关于本工单的历史版本

本工单 2026-08 之前的版本贴过一段 `fork_branch` 源码，其中含
`await self.restore_snapshot(from_snapshot_id)`。**该行已在 PR #15 删除**，
现在那个位置是解释"为什么不能 restore"的注释。旧版工单还建议让
`SnapshotManager` 直接 `import repository` 去建场景 —— 那违反契约 8。
**两处均已在本版修正，请勿参照旧版实现。**

---

## 阶段 A（前置）：给可变状态补上分支维度

> 目标：让 I3 在架构上成为可能。**本阶段不改变任何用户可见行为**，
> 是纯粹的地基改造，可以独立合入、独立回归。

### A.1 长期记忆 collection 加分支维度

`backend/memory/long_term.py`：

```python
def __init__(self, character_id: str, project_id: str, branch_id: str = ""):
    ...
    # 分支后缀让 IF 线与主线各自持有独立向量集合（工单08 I3）。
    # 留空 = 项目级共享，仅供跨分支只读场景使用。
    suffix = f"__{branch_id.replace('-', '')}" if branch_id else ""
    self.collection_name = f"char_{character_id.replace('-', '')}{suffix}"
```

`backend/memory/memory_manager.py` 的 `__init__` 同步透传 `branch_id`。

**两个生产构造点必须跟上**（其余在 `tests/`）：
- `backend/services/orchestrator.py` 的 `build_character_agents` → 传 `scene.branch_id`，
  因此该函数签名需增加 `branch_id` 参数；
- `backend/services/inspection.py` 的 `inspect_character` → 传调用方解析出的分支，
  否则面板查历史分支时对不上。

### A.2 fork 时复制 collection

新分支的第一场必须"继承"来源分支在时点 `S` 之前的长期记忆，否则角色会失忆（违反 I1）。
在 `SnapshotManager` 新增：

```python
async def clone_collections_for_branch(self, snapshot_id: str, new_branch_id: str) -> None:
```

从 `S.chroma_checkpoint` 里逐个读出 `char_{cid}` 集合的文档，写入
`char_{cid}__{new_branch_id}`。

**契约 6**：`Snapshot.chroma_checkpoint` 可能为空字符串（Chroma 不可用，或拷贝时按降级
路径失败）。此时**跳过复制并 `logger.warning`**，不得抛异常打断 fork。

### A.3 角色卡 `current_*` 降级为展示缓存

CLAUDE.md §4.2 陷阱 10 已经把它定义成"展示缓存"，但 `_apply_character_states`
仍在 rollback 时写它，而它是项目级单值 —— 两条分支交替推进会互相覆盖。

**本阶段只做一件事**：在 `_apply_character_states` 的 docstring 里写清"这是展示缓存，
不是权威数据源，权威值在快照里"，并确认**没有任何运行路径把它当权威值读**。

> `build_character_agents` 目前先读角色卡、再用 `state` 覆盖那四个字段，
> 已经是"快照优先"，符合要求。**验证并记录即可，不要顺手重构。**

### A.4 Kuzu 图谱：本阶段不做

图谱只在 GraphRAG 构建阶段写入一次，之后全程只读（见本目录 README 第 2 节）。
既然内容不变，"共享"与"隔离"效果等价，**当前无实际影响**。

⚠️ **但工单 06（场景结束后动态回写图谱）一旦落地，图谱就变成可变状态，I3 会立刻破。**
请在工单 06 里登记本条为前置约束。

### A.5 阶段 A 验收

1. `pytest tests/ -q` 全绿（重点：`test_character_agent.py`、`test_scene_engine.py`、
   `test_orchestrator.py`、`test_inspection.py` 里构造 `MemoryManager` 的地方）。
2. 新增单测：同一 `character_id` + 不同 `branch_id` 构造的两个 `LongTermMemory`，
   各写一条、各检索，断言互不可见。
3. 现有项目（`branch_id` 传空）行为不变 —— collection 名仍是 `char_{cid}`，
   **不需要数据迁移**。

---

## 阶段 B：把两套分叉实现收敛成一个

### B.1 新增唯一分叉原语

放在 `backend/services/orchestrator.py`（**契约 8**：跨模块编排只允许在这里；
`SnapshotManager` 不得 import `repository`）：

```python
async def fork_from_snapshot(
    project_id: str,
    snapshot_id: str,
    branch_name: str = "",
    conditions: dict | None = None,
    director_notes: str = "",
) -> tuple[Branch, Scene]:
```

实现要点，逐条对应不变量：

1. `snap = await sm.get_snapshot(snapshot_id)`；为 `None` 抛 `SnapshotNotFoundError`。
   **只读，绝不调 `restore_snapshot()`**（I2）。
2. `branch = await sm.fork_branch(...)`，`parent_branch_id = snap.branch_id`（I4）。
   `branch_name` 为空时自动生成（如 `f"回滚重演 · {src.name}"`）。
3. 调阶段 A.2 的 `clone_collections_for_branch(snapshot_id, branch.branch_id)`（I3）。
4. 取来源场景 `src = await repository.get_scene(snap.scene_id)`。
   **容错**：抛 `SceneNotFoundError` 时降级为用 `snap.character_states.keys()` 作为
   参演角色、其余字段留空 —— 快照可以比场景活得久（场景可被删）。
5. 构造 `Scene₀`：

   | 字段 | 取值 | 理由 |
   |------|------|------|
   | `branch_id` | `branch.branch_id` | I4 |
   | `parent_scene_id` | `snap.scene_id` | I4 |
   | `participating_characters` / `location` / `max_turns` | 承自 `src` | — |
   | `initial_conditions` | `{**src.initial_conditions, **(conditions or {})}` | I5，新条件覆盖旧条件 |
   | `speaker_mode` | 承自 `src.speaker_mode` | **§4.2 陷阱 3**：所有新建 Scene 的地方都要跟，漏传会静默退回 round_robin |
   | `snapshot_id_before` | `""` | **契约 2 的例外**：留空才能让引擎为新线重新打前置快照 |
   | `restore_snapshot_id` | `snapshot_id` | **契约 4**：I1 的唯一正确实现 |
   | `status` | `PENDING` | 不自动开跑 |
   | `name` | `f"{src.name}（{branch_name}）"` | 便于在场景列表里辨认 |

6. `await repository.save_scene(scene)`，返回 `(branch, scene)`。

> ⚠️ **不要复用 `create_scene_from_config()`** —— 它不设
> `restore_snapshot_id` / `parent_scene_id`，用了就会破 I1 和 I4。

### B.2 rollback 改为调用它

`apply_decision` 的 rollback 分支当前手工构造重演场景，且**落在原 `branch_id` 下**（I4 ✗）。
改为：

```python
branch, new_scene = await fork_from_snapshot(
    scene.project_id, target, conditions=decision.next_initial_conditions or {}
)
```

必须保持的两点：
- 目标快照缺失时**不持久化决策**，允许用户补 ID 后重试（现有行为，契约 5）；
- `_apply_character_states` 仍需调用（角色卡展示缓存要跟上），但已降级为非权威。

**语义变化需同步 CLAUDE.md §6.3 与 §12.1**：rollback 从此产生新分支。

### B.3 API 层

`backend/api/branches.py` 的 `fork_branch` 路由改为调 orchestrator，返回体扩展：

```python
branch, scene = await orchestrator.fork_from_snapshot(
    project_id, snapshot_id, req.branch_name, req.new_conditions, req.director_notes
)
return ApiResponse.ok({"branch": to_dict(branch), "scene": to_dict(scene)})
```

⚠️ **这是破坏性的响应体变更**（原来直接返回 `Branch` 的字段）。
必须同步改 `frontend/src/types/index.ts` 与 `frontend/src/api/client.ts`，
并更新 CLAUDE.md §8 里 `POST /snapshots/{snapshot_id}/fork` 那一行的说明。

### B.4 `fork_conditions` 字段的去留

条件既然已合并进 `Scene₀.initial_conditions`，`Branch.fork_conditions` 就成了纯冗余。

**保留**（不删），但降级为**溯源元数据**，在 `models.py` 的字段旁加一行注释说明
"权威值在 `Scene₀.initial_conditions`，此处仅供分支树展示"。
删字段要走 §5.4 三步 checklist 且牵连前端类型，收益不抵成本。

### B.5 前端

`frontend/src/pages/Director.vue`：

1. `confirmFork` 当前硬编码传 `{}`，**补一个条件输入框**（最简：每行 `key=value` 的
   textarea，解析成 `Record<string, string>`）。不补的话 I5 在 UI 上永远验证不了 ——
   旧版工单的验收步骤"用现有前端 UI 传 new_conditions"就是因此而做不到的。
2. fork 成功后：切 `branchId` 到新分支 → `refreshBranchData()` →
   **`sceneStore.attachScene(scene.scene_id)`**。

> ⚠️ **绝对不要用 `joinScene`**（旧版工单的建议）。`joinScene` 会调 `/start`，
> 用户点一下"分叉"就直接烧掉一整场 LLM 并写死快照与评估。
> 见 CLAUDE.md §9 "`attachScene` 与 `joinScene` 不可混用"。

### B.6 阶段 B 验收

**自动化**（`tests/test_orchestrator.py`，缺一不可）：

| 用例 | 断言 |
|------|------|
| `test_fork_creates_branch_and_pending_scene` | `scene.status == "pending"`；`branch_id` 是新分支；`parent_scene_id == snap.scene_id`（I4） |
| `test_fork_merges_conditions` | `conditions={"tension": "高"}` 时 `scene.initial_conditions["tension"] == "高"`，且来源场景原有的键仍在（I5） |
| `test_fork_sets_lazy_inheritance` | `scene.snapshot_id_before == ""` 且 `scene.restore_snapshot_id == snapshot_id`（I1 的必要条件） |
| `test_fork_does_not_touch_source_branch` | fork 前后来源场景的 `data_json` 逐字节相同；项目目录下 `chroma_db/` 的既有文件未被删除（I2，可复用 `test_rollback_does_not_wipe_shared_long_term_memory` 的写法） |
| `test_rollback_creates_new_branch` | rollback 产生的新场景 `branch_id != 原 scene.branch_id`（I4） |
| `test_fork_inherits_speaker_mode` | 来源场景 `speaker_mode="selector"` 时新场景一致（§4.2 陷阱 3） |
| `test_fork_survives_missing_source_scene` | 删掉来源场景后 fork 仍成功，参演角色取自 `snap.character_states` |

> **测试陷阱**（本仓库已踩过两次）：
> - 每个用例用**独立的** `project_id` / `scene_id` 后缀。复用同一个 `scene_id` 会命中
>   decisions 表的幂等重放，用例假通过。
> - 测 I2 / I3 前必须先断言**前置条件成立**（例如 `snap.chroma_checkpoint != ""`），
>   否则"什么都没有所以什么都没被删"也会通过。

**手工**：跑一场 → 从后置快照分叉并填条件 → 确认新分支下出现一个 pending 场景且界面
**没有自动开跑** → 手动开跑后检查角色开场状态与快照一致 → 回到主线再跑一场 →
检索两边长期记忆，确认互不污染（I3）。

---

## 2. 涉及文件

**阶段 A**
- `backend/memory/long_term.py`、`backend/memory/memory_manager.py`（`branch_id` 参数）
- `backend/services/orchestrator.py`（`build_character_agents` 签名）
- `backend/services/inspection.py`（构造点透传）
- `backend/snapshot/snapshot_manager.py`（`clone_collections_for_branch`）
- `tests/test_character_agent.py`、`tests/test_scene_engine.py`（构造点）

**阶段 B**
- `backend/services/orchestrator.py`（`fork_from_snapshot` + rollback 分支）
- `backend/api/branches.py`（路由改调 orchestrator）
- `backend/models.py`（`Branch.fork_conditions` 注释）
- `frontend/src/pages/Director.vue`、`frontend/src/stores/director.ts`、
  `frontend/src/types/index.ts`、`frontend/src/api/client.ts`
- `CLAUDE.md` §6.3 / §8 / §12.1（rollback 建新分支、fork 返回体、删掉已修复的缺陷条目）

## 3. 明确不做

| 项 | 理由 |
|----|------|
| 动 `SnapshotManager.restore_snapshot()` | 它已是 dead code（§12.2）。**不要修它，也不要接回任何写路径**，本工单让它继续闲置 |
| Kuzu 图谱的分支隔离 | 见 A.4，当前只读无影响；归工单 06 的前置 |
| `Branch.scenes` 写回 | 归工单 03 可选目标 6，与本单正交 |
| 改 `continue` 分支 | 按 0.3 结论 2，它不是分叉 |
| 分叉后自动开跑新场景 | fork 是探索性操作，不该有隐式的整场 LLM 成本 |
