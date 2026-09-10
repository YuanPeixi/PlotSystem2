# 工单 18 前置约束（临时文件）

> **状态**：临时。工单 18「导演场记板 / 分镜稿持久化」正式建单时，把本文件内容并入
> `18-storyboard.md` 的「线索」与「红线与已排除」两节，然后删除本文件。
>
> **为什么单独放一个文件**：这三条来自 PR #18 的 review，是 18 的前置技术约束，
> 不是 18 本身的目标。写进 `NOTES.md#t18` 会把"排期理由"和"实现约束"混在一起；
> 直接建 18 又违反 `NOTES.md#t18` 的既有判断（"等 04 / 28 落地后再写，接口会受其
> 实现细节影响"）—— 而这三条恰恰会**影响 18 的接口选型**，所以更不该现在定死。

**来源**：PR #18（工单 28 收尾）的 code review，编号沿用该次 review 的 #11 / #12 / #14。
**共同点**：都不影响正确性，都落在「导演历史的读取代价与存储形态」上，
而 18 的分镜稿会直接坐在这条路径上。现在单独优化，18 落地时很可能推翻重做。

---

## 约束 1：`_story_records` 是 N+1 查询（原 #11）

**位置**：`backend/services/orchestrator.py`，`_story_records`

每次调用都 `list_scenes(project_id)` 拉全项目场景（**反序列化时带上每一场的完整
`dialogue_log`**），再沿谱系对每个祖先逐个 `get_evaluation`（单行 SELECT）。

`run_scene` / `apply_decision` / `_story_context` 各调一次，谱系长 50 场即 50 次单行查询。

**对 18 的意义**：分镜稿如果也按"沿谱系回溯"的方式读取，会叠加在同一条路径上。
建单时应一并决定：是继续按需回溯，还是给导演历史建一个物化视图 / 批量查询接口
（`SELECT ... WHERE scene_id IN (...)`）。

---

## 约束 2：`story_history` 使快照列表接口 O(N²)（原 #12）

**位置**：`backend/snapshot/snapshot_manager.py`

`story_history` 全量副本随每个快照写进 SQLite 的 `data_json`；`list_snapshots` 把整行
`data_json` 读回内存。50 场分支 × 每场 2 个快照 = 100 个快照，第 N 个快照带 N 条完整
`SceneEvaluation`（含 synopsis 与最多 20 条 `unresolved_threads`），合计 O(N²) 条记录。

而 `api/branches.py` 的投影只用到 `snapshot_id` / `label` / `created_at` /
`character_count` 四个字段 —— 整份历史被读出又丢弃。

**对 18 的意义**：分镜稿也要随快照版本化（`NOTES.md#t18` 的设想是"随快照一起
版本化"），会让同一个问题再放大一倍。建单时应决定存储布局：继续塞 `data_json`、
拆独立表、还是快照只存引用。

---

## 约束 3：导演历史是裸 `list[dict]`，违反 §10.1（原 #14）

**位置**：`backend/models.py` 的 `Scene.inherited_story_history` 与 `Snapshot.story_history`

两者类型都是 `list[dict]`，字段名只在 `orchestrator._story_record` 里以字符串字面量
存在。CLAUDE.md §10.1 要求"内部数据用 `@dataclass`（集中在 `models.py`）"。

**具体风险**：`_story_context` 用 `record['name']` 与 `ev.get('story_progress', -1)` 读取。
将来若 `_story_record` 改写成 `'scene_name'`，前者抛 `KeyError`（还算响亮），
**后者会静默退回 `-1`，把有效进度当成 `PROGRESS_UNAVAILABLE`** —— 决策阈值跟着错，
且没有任何报错。

**对 18 的意义**：这是三条里**最该在 18 之前想清楚**的。分镜稿的数据结构选型会直接
决定要不要把导演历史一起 dataclass 化 —— 两者都是"导演的持久化记忆"，用两套不同的
序列化风格会长期折磨。改造要同步 `repository._deserialize_*`、快照 meta、
`frontend/src/types/index.ts`（走 CLAUDE.md §5.4 三步 checklist）。

---

## 建单时的检查项

- [ ] 三条约束已并入 `18-storyboard.md` 的「线索」/「红线与已排除」
- [ ] 约束 3 的结论（是否 dataclass 化）已确定，因为它决定 18 的数据结构
- [ ] 本文件已删除
- [ ] `NOTES.md#t18` 补一句指向 18 工单文件，去掉"等 04/28 落地"的待办措辞
