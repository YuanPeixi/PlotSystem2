---
paths:
  - "backend/models.py"
  - "backend/services/repository.py"
  - "backend/services/world_repository.py"
  - "backend/services/world.py"
  - "backend/services/storyboard.py"
  - "backend/services/objects.py"
  - "backend/services/environment.py"
  - "backend/utils/db.py"
  - "backend/utils/serializer.py"
  - "backend/utils/usage.py"
  - "backend/snapshot/snapshot_manager.py"
  - "backend/snapshot/models.py"
  - "backend/scene_engine/scene_config.py"
  - "frontend/src/types/index.ts"
---

# 持久化与数据模型规则

本文件管：数据模型的存放位置与"不落库"语义、SQLite / 文件系统布局、进程内易失状态、
**修改数据模型的三步 checklist【契约】**、陷阱 5。
目录：模型总览（原 §4.1）· SQLite（原 §5.1）· 文件系统（原 §5.2）· 进程内易失状态（原 §5.3）·
三步 checklist（原 §5.4）· 陷阱 5。

**唯一定义源是 `backend/models.py`**，这里只讲代码看不出来的语义；字段清单读代码。

---

## 模型总览：存放位置与语义（原 §4.1）

| 模型 | 存放位置 | 要点（代码看不出的） |
|------|---------|------|
| `Project` | SQLite `projects` | `narrative_goal` 是只读锚点，只有用户能写（陷阱 15，见 `director-story.md`）；`world_schema_version` 缺省按 1 读取 |
| `CharacterCard` | **文件** `characters/{cid}.json` | 不入库；`current_*` 只是展示缓存；`LoreEntry` 内嵌其中 |
| `CharacterState` | 快照目录 JSON | 某时刻的快照态，含短期缓冲 / 事件摘要 |
| `CharacterInspection` | 不落库 | Inspection 层只读组装结果 |
| `WorldState`（schema 1） | **文件** `world_state/{branch_id}.json` | 分支级、可人工编辑，读取侧要压预算（见 `world-environment.md`） |
| `WorldRevision` / `WorldFact` / `WorldObservation`（schema 2） | SQLite `world_revisions` | 不可变版本；快照引用版本 ID，不存副本 |
| `WorldOperation` / `WorldEvent` | SQLite `world_events` | 显式 set/unset 与已提交事件 |
| `Storyboard` / `StoryBeat` / `ForkOrigin` | **文件** `storyboard/{branch_id}.json`，快照带时点副本 | 分支级；**只进导演 prompt** |
| `StoryboardPatch` | 内嵌于 `SceneEvaluation` | 相对导演读到的那一版 |
| `StoryboardView` | 不落库 | GET/PUT 响应，含 `goal_stale` |
| `Scene` / `DialogueTurn` | SQLite `scenes`（轮次内嵌） | `snapshot_id_before` / `restore_snapshot_id` 语义见陷阱 1、2（`scene-engine.md` / `director-story.md`） |
| `ActionIntent` | 随轮次内嵌 | 只给导演与用户看，不进角色视野 |
| `LLMUsageStat` | 随场景 / 评估内嵌 | `Scene.llm_usage` 记运行期，`SceneEvaluation.llm_usage` 记评估 |
| `WorldObject` | **文件** `objects/{object_id}.json` | 项目级；隐藏规则只进导演与环境层 |
| `SceneLineage` | 不落库 | 谱系回溯用的场景字段投影，**只读、不可存回**（存回会抹掉整场对白） |
| `SceneConfig` | 不落库 | 导演规划产物，运行时构造 |
| `SceneEvaluation` | SQLite `evaluations` | |
| `StoryRecord` | 内嵌于场景 / 快照 | 导演历史的一条；`threads_known` 随记录序列化 |
| `DirectorDecision` | SQLite `decisions` | `next_*` 覆盖字段全为 `None` 才是"保持 AI 自动规划" |
| `AutoPilotSession` / `AutoPilotStep` | **进程内存，不落库** | 刻意的，见陷阱 22 |
| `Snapshot` / `Branch` / `BranchTree` | SQLite `snapshots` / `branches` + 快照目录 | |
| `MemoryChunk` / `MemorySnapshot` | 运行时 | 记忆检索与序列化载体 |

---

## SQLite（`data/projects.db`，10 张表）（原 §5.1）

`projects` / `branches` / `scenes` / `snapshots` / `evaluations` / `decisions` / `outputs` /
`world_revisions` / `world_heads` / `world_events`（后三张为 schema 2 新增）。

**【契约】统一模式：每张表的 `data_json` 列存放整个 dataclass 的 JSON，是唯一真相源；
`name` / `status` / `branch_id` / `created_at` 等列仅用于索引、过滤与 CAS。**
所有读取都从 `data_json` 反序列化；只更新列而不更新 `data_json`，对 API 完全不可见。

**例外 1**：`scenes.status` 列会承载 CAS 瞬态值 `deciding`，且**刻意不写入 `data_json`**
（契约 5）。该列值域比 `SceneStatus` 枚举多一个。

**例外 2**：`snapshots` 表的 `data_json` 只存列表投影（id / scene_id / branch_id / label /
created_at / `character_ids`），**快照的唯一真相源是快照目录下的 `meta.json`**。
原因：整份快照含随谱系增长的导演历史副本，列表接口会 O(N²)。`list_snapshots` 在 SQL 侧投影，
兼容旧的整份行（用 `json_each` 取 `character_states` 的键）。`character_ids` 不可删：
`inspection._latest_snapshot_id` 靠它找"某角色最近出现的快照"。详见
`docs/fix-tickets/18-storyboard.md`（工单18 D3）。

---

## 文件系统 `data/projects/{project_id}/`（原 §5.2）

```
characters/{character_id}.json    角色卡不入库
world_state/{branch_id}.json      分支级世界变量（schema 1），不入库，可人工编辑
storyboard/{branch_id}.json       分支级导演分镜稿，不入库
objects/{object_id}.json          项目级物件，不入库，可人工编辑
seed_texts/                       原始种子文本
kuzu_db                           当前 Kuzu 版本下是【单个文件】，不是目录
chroma_db/                        向量库
snapshots/{snapshot_id}/
    meta.json                     快照的唯一真相源
    character_states/{cid}.json
    chroma_collections/
build_status.json                 构建进度（供重启后对账）
```

角色卡无 `delete_character`；删项目直接 `rmtree` 整个项目目录。
凡是"人工可编辑"的文件，读取侧都要自带预算 / 容错（不信任文件内容），见各自规则文件。

---

## 进程内易失状态（原 §5.3）

`_active_scenes`（并发守卫）、`_running_engines`（暂停/中断）、`_build_status`（有磁盘兜底）、
`_branch_locks`（分支级文件——世界变量与分镜稿——的读-改-写临界区，同一把锁，只能有一把）、
`_pending_world_patch`（后置快照补写窗口守卫）、`_autopilot_sessions` / `_autopilot_locks`、
`_object_locks`、`_background_tasks`（持有 `create_task` 引用防回收）、
`events._subscribers`（SSE 订阅者）、每个 `MemoryManager` 的短期与事件记忆。

这些都是进程内的，**同属契约 9**（单 worker）。新增进程内状态要登记到这里。

---

## 修改数据模型的三步 checklist（原 §5.4）【契约】

1. 改 `backend/models.py` 的 dataclass；
2. **同步改对应的反序列化函数**，漏这步字段会静默丢失：
   - `services/repository.py`：`_deserialize_card` / `_deserialize_scene` / `_deserialize_turn` /
     `_deserialize_actions` / `_deserialize_object` / `deserialize_story_history` /
     `deserialize_storyboard` / `_deserialize_world_state`（及评估、决策的反序列化）；
   - `snapshot/snapshot_manager.py`：`_deserialize_character_state` 与 `get_snapshot`；
   - 共用的 `utils/usage.py::deserialize_usage`（场景与评估）、
     `services/environment.py::clamp_environment_state`（场景物件状态）、
     `services/world.py::deserialize_revision`（世界版本）；
3. 评估是否需要新增 SQL 列（只有需要索引 / 过滤 / CAS 时才加，普通字段靠 `data_json` 自动携带）。

前端有对应类型时同步改 `frontend/src/types/index.ts`。

反序列化通用要求：坏条目跳过并 warning、不得因一条坏数据让列表接口报 500；
时间戳统一走 `models.as_aware`、`created_at` 必须还原而不是每次读都换 `now()`（见 `python.md`、陷阱 16）。

---

### 陷阱 5 — `CharacterState.long_term_memory_snapshot` 恒为空字符串

`_collect_states()` 不写它，长期记忆不随快照序列化（它在 Chroma 里，按角色 + 分支隔离）。
不要依赖这个字段，也不要顺手去填。（同列入根 CLAUDE.md 第 12 节 dead code 清单。）
