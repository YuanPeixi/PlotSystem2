---
paths:
  - "backend/snapshot/**"
  - "backend/services/orchestrator.py"
  - "backend/services/inspection.py"
  - "backend/api/branches.py"
  - "backend/api/director.py"
  - "backend/memory/long_term.py"
  - "frontend/src/utils/branches.ts"
  - "frontend/src/pages/BranchMap.vue"
---

# 分叉原语、快照与状态作用域（规则文件）

本文件管：`fork_from_snapshot` 的不变量、快照/回滚的破坏性边界、状态作用域表。
决策与回滚入口见 `director-story.md` §2；Chroma 分支集合命名与老集合承接见 `memory.md`（陷阱 11 / 12）；世界层细节见 `world-environment.md`。

目录：1. 状态作用域与 `restore_snapshot` 破坏性（陷阱 10）；2. 分叉原语 `fork_from_snapshot`（原 §6.3.1）；3. 评估补写窗口内拒绝分叉。

---

## 1. 陷阱 10 — 状态的作用域不是统一的

这是 fork / rollback 语义分歧的根源：

| 状态 | 实际作用域 | 分支隔离 |
|------|-----------|---------|
| 快照里的 `CharacterState` | 分支/时点级 | 是 |
| 角色卡 `current_*` | 项目级单值（只当**展示缓存**） | 否 |
| 世界状态：schema 1 `world_state/{branch_id}.json`；schema 2 `world_heads` + 不可变 `world_revisions` | 分支级（schema 2 的版本可被多分支共享引用，写入各自生成新版本） | 是 |
| 分镜稿 `storyboard/{branch_id}.json` | 分支级 | 是 |
| Chroma 长期记忆（collection = `char_{cid}__{sha256(branch_id)[:24]}`） | 角色+分支级 | 是 |
| Kuzu 图谱 | 项目级单文件 | 否（目前只读，暂无影响；工单 06 落地前必须先解决） |

因此 **`SnapshotManager.restore_snapshot()` 是破坏性操作**：它 `rmtree` 后拷回旧副本，等于抹掉快照之后**所有分支**已积累的长期记忆与图谱，不可逆；也不恢复分支级世界/分镜稿，恢复出的状态是半对的。它已无生产调用方，加了 `confirm_destructive=True` 路障，**不要把它接回任何写路径**。
要让某一场从快照接上运行时记忆，**唯一正道是契约 4 的懒承接**（`Scene.restore_snapshot_id` + `inspection.resolve_scene_states`）。

## 2. 分叉原语 `fork_from_snapshot`（原 §6.3.1）

`fork(S, C, name) -> (Branch, Scene₀)` 是系统里**唯一**的分叉入口：`POST /snapshots/{id}/fork` 与 rollback 决策都走它。两套实现分叉过一次，其中一套就会悄悄退化（工单 08 结论 1）。

### 五条不变量

| 编号 | 名称 | 约束 |
|------|------|------|
| I1 | 起点一致 | `Scene₀.restore_snapshot_id = S`，靠契约 4 懒承接，**绝不 `restore_snapshot()`** |
| I2 | 无副作用 | 全程只读来源分支，只 INSERT 新分支/新场景；Chroma `PersistentClient` 打开时会维护文件，所以只能打开 checkpoint 的**临时副本**，不能直接打开权威快照目录（代价：分叉期间向量库磁盘峰值翻倍） |
| I3 | 相互隔离 | `clone_collections_for_branch()` 把 `S.chroma_checkpoint` 里**来源分支的全部角色集合**（不是本场参演名单，否则缺席者一分叉就失忆）搬进新分支集合（`collection_name_for(cid, 新分支)`），分页读 + 按客户端 `max_batch_size` 分批写；另以分支级初始化文件封住快照中不存在 collection 的角色和空起点。世界层与分镜稿同理（见下） |
| I4 | 可追溯 | `Branch.parent_branch_id = S.branch_id`；`Scene₀.parent_scene_id = S.scene_id` |
| I5 | 条件生效 | `Scene₀.initial_conditions = {**来源场景条件, **C}`；`Branch.fork_conditions` 仅溯源元数据，权威值在 `Scene₀.initial_conditions` |

### 分支级状态随分叉搬运（I3 的延伸）

- **schema 2 项目**（按 `project.world_schema_version`；项目记录缺失时按快照有无 `world_revision_id` 判断）：新分支经 `repository.initialize_branch_world` 引用快照的 `world_revision_id`，不可变版本被共享、后续写入各生成新版本。
  - **旧快照没有 `world_revision_id`**：新分支以空世界起步（warning），但把快照里的旧世界变量记为 `legacy_records`、并采用当前项目物件定义，**不猜历史物件状态**、不回读来源分支的当前世界；已迁移项目由此保留迁移基线。
  - 细节（观察、规则采用、占用）见 `world-environment.md`。
- **schema 1 项目**：`S.world_state_variables` 写进 `world_state/{新分支}.json`（分支级文件不随快照目录走，不搬就是"一分叉世界重置"）。空变量也照写，让起点是显式的空而不是"文件还没建"。
- **分镜稿**：`S.storyboard` 写进 `storyboard/{新分支}.json`，附确定性模板生成的 `fork_origin`（来源分支名、快照标签、IF 条件、`director_notes`）；**分叉不调 LLM**。旧快照无副本（`None`）时以空稿起步 + warning，不回读来源分支的当前分镜稿（陷阱 21）。
- **导演历史**：`Scene₀.inherited_story_history = deepcopy(snap.story_history or [])`，边界规则见陷阱 16（`director-story.md`）。
- **新场景字段**：`speaker_mode`（漏传 selector 静默退回轮询，陷阱 3）、`objects_present`、`environment_script` 均从来源场景搬运（陷阱 13）。

### 顺序与失败语义

- **记忆先搬、分支后建**：用预生成的 `branch_id` 在 `fork_branch` 之前复制记忆，失败（`MemoryError` → 500）就不会留下一条无记忆的孤儿分支。世界层与分镜稿的落盘同样排在 `fork_branch` 之前，同一理由。
- Chroma 不可用 / 快照不含向量库：按契约 6 只 warning 并记录空起点；Chroma 已安装且快照库存在但复制/打开失败则**必须中止**。
- `Scene₀` 恒为 `pending`、`snapshot_id_before=""`（契约 2 的例外，让引擎为新线重打前置快照），**不自动开跑**：分叉是探索性操作，不该隐含整场 LLM 成本。
- 来源场景已被删时降级：参演角色取自 `S.character_states`。
- 前端：分叉后只 `attachScene`，绝不 `joinScene`（见 `frontend.md`）。

## 3. 评估补写窗口内拒绝分叉

`fork_from_snapshot` 入口先查 `orchestrator._pending_world_patch`（经 `is_snapshot_pending_world_patch` 暴露），命中抛 `ConflictError`（409），提示稍后重试（评估通常几秒到十几秒）。

原因：后置快照在 `create_snapshot` 写入 `snapshots` 表那一刻就对分叉可见（fork 只读快照，不看 `scene.status`），而评估、`record_story_history`、世界/分镜稿补写要在其后才完成；分叉只读一次快照内容整份拷走，之后的补写不会再传播，新分支会**永久且无迹可查**地缺失本场结果。所以统一堵住整个窗口（story_history、分镜稿、schema 1 世界变量同一机制）。

- **窗口起点是快照创建、不是 scene 落库**：标记由引擎在创建前经 `on_after_snapshot` 挂上；不要挪回 `run()` 返回之后，也不要改用 `save_scene` 作为可见性起点。
- 摘除在评估 + 补写的 `finally`（成败均可）；机制细节见 `director-story.md` §1。
- 进程内状态，单进程假设（契约 9）。
- schema 2 下世界事实场中已事务提交，窗口只为导演历史与分镜稿副本而设；schema 2 同分支的执行占用另见 `world-environment.md`（暂停后分支已推进，原场需从快照分叉后继续）。

详见 `docs/fix-tickets/08-fork-branch-conditions.md`（含 `#fork-details`）与 `docs/fix-tickets/01-rollback-fix.md`。
