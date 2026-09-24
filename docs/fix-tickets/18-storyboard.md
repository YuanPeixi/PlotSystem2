# 工单18：导演分镜稿（storyboard）持久化

**优先级**：P2 ｜ **依赖**：17 ✅、28 ✅（07 ✅ 提供了可照抄的分支级状态范式）｜
**契约影响**：契约 1（分镜稿含导演全知信息，不得进入角色可见上下文）、契约 5（新写接口）

> 本单按新骨架写：只给目标、判据与红线，不给分步实现。
> §3 的数据结构是**语义要求**，字段名可以调整，但每个字段承担的职责不能丢。
> 设计背景（三层目标模型、两条禁令、28 已定口径）见
> [NOTES.md#director-goal](./NOTES.md#director-goal)，**那里已定的东西不要重新讨论**。

---

## 1. 现象

导演是整个系统里最重的角色，却是最健忘的一个：

1. **没有路线图**。导演每场只拿到主线目标（第 1 层，只读）与本场意图（第 3 层，一次性），
   中间缺一层"这条分支打算怎么走到目标"。连跑十场后，规划只剩"根据最近几场的梗概接着演"，
   长线伏笔全靠 `unresolved_threads` 这一个列表撑着，而它只记"还没收束什么"，
   不记"打算怎么收束、先后顺序是什么"。
2. **规划与评估看的历史不是同一份**。评估走 `orchestrator._story_records`（沿因果谱系回溯，
   分叉处按快照冻结），规划却走 `orchestrator.plan_scene` 里的 `list_scenes(branch_id)`
   取本分支最近 5 场 + 最近 3 份评估。后果：**从快照分叉出的新分支，首场跑完后导演规划
   下一场时完全不知道分叉点之前发生过什么**——评估那一侧却知道。
3. **分叉的说明到不了导演**。`POST /snapshots/{id}/fork` 收集了用户写的 `director_notes`
   与 IF 条件，存进 `Branch.director_notes` / `Branch.fork_conditions` 后就再也没人读——
   它们从未进入任何导演 prompt。导演在一条 IF 线上工作，却不知道这条线"如果"的是什么。

## 2. 为什么要做

- 这是"导演 Heavy Duty 却配套工具不足"的正面解法（NOTES#t18）。第 1 层锚点（28）
  解决了"没有引力"，但引力只告诉导演终点在哪，不告诉它路怎么走。
- 12（AutoPilot）与 22（MCTS / 多结局）都坐在它上面：无人值守连跑时，没有路线图的导演
  只会在动量里打转；而 MCTS 需要可比较的节点价值，分镜稿是其中一半。
- 现象 2、3 是**静默**的：没有报错，只表现为 IF 线上的导演"变笨了"，人工很难归因。

## 3. 判据（Definition of Done）

### 3.0 前置：还清导演历史的三笔技术债（行为不变，单独提交）

原记于 PR #18 review（编号沿用该次 review 的 #11 / #12 / #14）。三条都不影响正确性，
但都落在"导演历史的读取代价与存储形态"上，而分镜稿会直接坐在这条路径上——
先还债再建新东西，否则 18 落地时要推翻重做。

**D1 — 导演历史 dataclass 化**（原 #14，三条里最该先做的）

`Scene.inherited_story_history` 与 `Snapshot.story_history` 现在都是裸 `list[dict]`，
字段名只以字符串字面量存在于 `_story_record`，违反 `CLAUDE.md` §10.1。
**具体风险**：`_story_context` 用 `ev.get('story_progress', -1)` 读取——将来任何一处改了键名，
有效进度会**静默退回 `PROGRESS_UNAVAILABLE`**，决策阈值跟着错，且没有任何报错。

- 在 `models.py` 定义记录类型（语义：`scene_id` + 场景名 + 一份 `SceneEvaluation`），
  两个字段改为该类型的列表；
- **`None` 与 `[]` 的区分必须保留**（`None` = 旧数据请回溯，`[]` = 权威空历史，§4.2 陷阱 16）；
- 旧快照 `meta.json` 与旧 `scenes.data_json` 里的 `list[dict]` 必须能读回——
  反序列化侧兼容，**不写迁移脚本**；损坏条目降级跳过并 warning，不得让整个 `list_scenes` 500
  （PR #18 教训 3：降级要成片）。

**D2 — 消除 `_story_records` 的 N+1**（原 #11）

每次调用都 `list_scenes(project_id)` 拉全项目场景（**连带每一场的完整 `dialogue_log`**），
再沿谱系对每个祖先逐个 `get_evaluation`。`run_scene` / `apply_decision` / `_story_context`
各调一次，谱系 50 场即 50 次单行查询。

- 评估改为批量读取（`WHERE scene_id IN (...)`）；
- 谱系回溯所需的场景字段不应连带反序列化 `dialogue_log`；
- 维持 28 定下的"**不新增存储，评估时现算**"口径——不建物化视图。

**D3 — 快照列表接口不再 O(N²)**（原 #12）

`story_history` 全量副本随每个快照写进 `snapshots.data_json`，`list_snapshots` 把整行读回；
第 N 个快照带 N 条完整评估。而 `api/branches.py` 只用到四个字段。

- 已核实：**`snapshots.data_json` 只有 `list_snapshots` 在读**，`get_snapshot` 读的是
  `meta.json`。因此索引行可以只存列表所需的投影，`meta.json` 仍是快照的完整真相源；
- ⚠️ **投影必须保留角色 id 列表**：`inspection._latest_snapshot_id` 靠
  `row["character_states"]` 判断某角色出现在哪些快照。删掉它 Inspection 面板会静默退回角色卡；
- 本单新增的分镜稿副本同样**只进 `meta.json`**，不进索引行。

**D0 判据**：改造前后，同一组场景/快照上的 `_story_context` 三元组输出逐项相等；既有测试全绿。

### 3.1 分镜稿的数据形态

分支级、导演可写、每次改动留痕。语义上至少包含：

| 字段（名称可调） | 语义 |
|---|---|
| `outline` | 路线图：有序的节拍列表。每个节拍有标题、说明、状态（`planned` / `done` / `dropped`）、在哪一场完成或放弃 |
| `memo` | 导演的长期备忘（人物弧光、已埋伏笔的打算、刻意留白的东西）。自由文本 |
| `goal_revision` | 这份路线图是对照**哪个版本的主线目标**写的（`models.goal_revision()`） |
| `fork_origin` | 本分支从哪里分叉、改变了什么条件、用户写了什么备注。分叉时生成 |
| `changelog` | 改动留痕：哪一场 / 谁（`director` / `user` / `fork`）/ 改了什么的一句话摘要。**限条数，不进 prompt** |

存放照搬 `WorldState`（工单07）：

- 权威值是分支级文件 `data/projects/{pid}/storyboard/{branch_id}.json`，不入 SQLite；
  文件不存在 = 空分镜稿，不是错误；`branch_id` 为空拒绝写入（同 `save_world_state`）；
- **路径预算**：新路径层级要量一量（`CLAUDE.md` §10.1，整个数据目录约 65 字符余量）；
- 快照的 `meta.json` 带一份时点副本（前置、后置都带），**不进索引行**（见 D3）。

### 3.2 预算是硬约束，两道闸门

分镜稿进的是**每一次**规划与评估的 prompt，与世界变量同一条教训（§4.2 陷阱 19）：

- 写入侧：合并导演的 patch 时，同时限节拍条数、单条 token、`memo` token、总 token；
  超限要 warning，**不得静默丢弃**；
- 读取侧：文件摆在项目目录里、可被人工编辑，**反序列化时就压回同一预算**，
  只压不写回（同 `_deserialize_world_state`）；
- 一切进 prompt 的文本塌成单行渲染（键与值都塌，PR #20 的教训：只塌值做不全）。

### 3.3 导演读

- 规划（`plan_scene`）与评估（`evaluate_scene`）的 prompt 都注入分镜稿（路线图 + 备忘 + 分叉说明），
  放在主线目标**之后**，并在提示词里写明"路线图服务于主线目标，二者冲突时以主线目标为准"；
- **规划的历史改为与评估同源**：规划也走谱系（`_story_context` 那一套，分叉处按冻结副本截止），
  不再按 `list_scenes(branch_id)` 取。修掉 §1 现象 2；
- `goal_revision` 与当前主线目标不一致时，**不自动删、不静默沿用**：prompt 里明确标注
  "以下路线图基于旧版主线目标，请据新目标重排"，由导演在下一次评估的 patch 里改写；
- 分镜稿**只进导演 prompt**（红线 R1）。

### 3.4 导演写

- 评估输出新增一个 patch 字段（节拍的新增 / 标记完成 / 标记放弃 / 改写，外加 `memo` 改写），
  **随评估同一次 LLM 调用产出**，不额外调用；
- **评估解析失败时 patch 必须为空**，分镜稿原样不动——与 `world_state_delta` 同理，
  绝不能让一次失败的调用伪装成一次真实的规划调整；
- patch 的数值与布尔字段走 `_parse_number` / `_parse_bool`（§4.2 陷阱 18，28 教训 4）；
- 合并落盘是"读-改-写"，**临界区必须从重读开始**，复用 `_world_state_lock` 的分桶机制
  （可改名为通用的分支锁，但只能有一把）。理由与陷阱 19 完全相同：`run_scene` 开场读的那份
  与写回之间隔着整场 LLM，同分支两场并发会互相整份覆盖；
- 合并后**补写后置快照**（同 `record_world_state`）。补写完成前该快照必须处于
  `_pending_world_patch` 守卫下（它已覆盖评估+补写整个窗口，只需确认分镜稿补写落在窗口内）；
- 每次合并写一条 `source=director` 的 changelog。

### 3.5 分叉与继承

- `fork_from_snapshot` 把快照里的分镜稿副本写进新分支文件，**排在 `fork_branch` 之前**
  （同世界变量：失败就不该留下一条"导演失忆"的分支）；
- 同时生成 `fork_origin`：来源分支名、来源快照标签、IF 条件、`director_notes`。
  **确定性模板生成，不调 LLM**（红线 R4）。修掉 §1 现象 3；
- rollback 走同一原语，自动获得上述行为，不另写；
- 旧快照没有分镜稿副本时 warning + 以空分镜稿起步（同 `story_history is None` 的降级口径），
  **不回读来源分支的当前分镜稿**——那是分叉之后才写的，会越过继承边界（28 教训 8）；
- `next_scene` 在同一分支上，天然读分支文件，无需搬运。

### 3.6 用户读写

- `GET /projects/{pid}/branches/{bid}/storyboard`：无记录返回空分镜稿，不是 404（同 world-state）；
- `PUT` 同路径：用户整份替换 `outline` / `memo`，写一条 `source=user` 的 changelog，
  过同一套预算校验（422 而非静默截断）；
- **幂等**（契约 5）：整份替换本身幂等，但要防"覆盖别人刚写的"——请求带上读取时的版本
  （`updated_at` 或修订号），不匹配返回 409。评估写入与用户编辑会并发；
- 用户**不能**经这个接口写 `goal_revision` / `fork_origin` / `changelog`，它们由后端维护。

### 3.7 前端

- `Director.vue` 增加分镜稿面板：分叉说明置顶、路线图按状态区分展示、可编辑、可展开 changelog；
- 切分支时面板跟着切（§9 "切分支要连当前场景一起切"的同族问题）；编辑草稿要有归属分支，
  切分支时清空（28 教训 5：草稿没有归属项目）；
- 409 冲突时提示重新加载，不自动覆盖；
- `frontend/src/types/index.ts` 同步。前端测试必须能被 `npm test` 选中。

## 4. 红线与已排除

**红线**

| 编号 | 约束 | 理由 |
|---|---|---|
| R1 | 分镜稿**绝不**进入 `CharacterAgent.build_system_prompt()`、selector 打分 prompt 或任何角色可见上下文 | 它含导演对全部角色 `unknown_facts` 的安排（契约 1）。一旦泄漏，角色会"知道剧本" |
| R2 | 导演的任何写路径都**不得**改 `Project.narrative_goal`；分镜稿里不得出现能覆盖主线目标的字段 | 禁令 1（§4.2 陷阱 15）。分镜稿就是导演的可写空间，写空间之外不许伸手 |
| R3 | 不新增 `DecisionType.END`；分镜稿**不进** `make_decision` 的阈值规则 | 禁令 2；本单只做记忆，不做决策信号。要让路线图影响决策归 12 / 22 |
| R4 | 分叉路径上不调 LLM | 分叉是探索性操作，刻意不隐含 LLM 成本（§6.3.1） |
| R5 | 28 已定口径不动：`unresolved_threads` 仍由评估逐场给出，**不并入**分镜稿；推进度仍现算 | NOTES#director-goal "28 落地时定下的口径"。线索是"还欠什么"，路线图是"打算怎么走"，两者互补不替代 |
| R6 | 分镜稿副本不进 `snapshots.data_json` 索引行 | 否则 D3 刚还的债立刻翻倍 |
| R7 | 不调 `restore_snapshot()` | §4.2 陷阱 10 |

**已排除的方案**

1. **单独一次 LLM 调用来更新分镜稿**（评估之后再调一次）—— prompt 更干净，但每场多一次调用，
   且两次调用之间看到的上下文不一致。随评估产出 patch 即可，与 `world_state_delta` 同构。
2. **分镜稿存 SQLite**（新表或 `branches.data_json`）—— 世界变量已确立"分支级文件 + 快照副本"
   范式，两套存储形态会让 fork/快照/补写各写两遍。且文件可人工编辑本身是特性。
3. **把 `unresolved_threads` 迁进分镜稿**—— 违反 R5；且会波及 28 的 `threads_found` 语义与前端。
4. **分叉时让 LLM 生成"与原线的差异说明"**—— 违反 R4。确定性模板 + 下一次评估时导演
   自己在 patch 里补写理解，已经够用。
5. **给导演历史建物化视图**（D2）—— 违反 28 "不新增存储，评估时现算"的口径；批量查询足够。
6. **导演历史保持 `list[dict]`，只给分镜稿用 dataclass**—— 两套序列化风格并存会长期折磨，
   且 D1 的静默退回风险是真实的。

## 5. 验收

| 用例 | 断言 |
|------|------|
| D0 等价回归 | 构造含分叉、continue、手建场景的谱系，改造前后 `_story_context` 输出逐项相等 |
| D1 旧数据兼容 | 旧格式 `list[dict]` 的快照/场景能读回；`None` 与 `[]` 读回后仍可区分；一条损坏记录不致 `list_scenes` 失败 |
| D2 查询次数 | 谱系 N 场时评估查询次数为常数（可 patch 计数），而非 N |
| D3 投影 | `list_snapshots` 行里不含 `story_history` / 分镜稿；`inspection` 的"最近出现快照"解析结果不变 |
| 契约 1 | 分镜稿里放一个独特标记串，跑一场，断言它出现在导演 prompt、**不出现**在任何角色与 selector 的 prompt |
| 解析失败不写 | 评估返回非 JSON，断言分镜稿文件与后置快照副本均未变 |
| 并发不覆盖 | 同分支两场，A 的 patch 先落盘、B 以空 patch 收尾，断言 A 的改动仍在（需让 B 持有开场旧副本，否则是假通过） |
| 分叉继承边界 | 从快照 S 分叉后，来源分支再写分镜稿，断言新分支看不到这次写入；旧快照无副本时新分支为空且有 warning |
| 分叉说明 | 带 `director_notes` 与条件分叉，断言首场规划/评估 prompt 含该说明，且分叉过程零 LLM 调用 |
| 规划同源 | 分叉分支首场跑完后规划下一场，断言规划 prompt 含分叉点之前的梗概 |
| 预算两道闸 | 手写超限文件 → 读取时被压回预算、文件本身不变；patch 超限 → 有 warning |
| 目标版本 | 改 `narrative_goal` 后，prompt 出现"基于旧版主线目标"标注，分镜稿未被删 |
| 用户编辑 | PUT 版本不匹配返回 409；超预算返回 422；成功后 changelog 多一条 `source=user` |

> 另见 `CONVENTIONS.md` §7 的五条"假通过"。尤其"并发不覆盖"与"分叉继承边界"两条，
> 构造的写入必须来自**两条不同路径**，否则走不到被测分支。

**手工**：真 LLM 连跑 5 场以上，观察路线图节拍是否随场次被标记完成、导演是否据此规划；
分叉一条 IF 线，确认首场规划提到了分叉条件。

## 6. 线索（起点，不是清单）

- 导演历史：`backend/services/orchestrator.py` 的 `_story_record` / `_story_records` /
  `_merge_story_records` / `_story_context`；`record_story_history` 的调用点
- 规划历史：`orchestrator.plan_scene` 与 `_recent_scene_results`
- 快照：`backend/snapshot/snapshot_manager.py` 的 `create_snapshot` / `_index_snapshot` /
  `get_snapshot` / `list_snapshots` / `record_world_state`（补写范式）；
  `backend/services/inspection.py` 的 `_latest_snapshot_id`（D3 的隐藏消费方）
- 范式可照抄：工单07 的 `WorldState` 全链路 —— `services/world_state.py`（纯函数、读写两侧共用）、
  `repository.get/save_world_state`、`_apply_world_delta`（锁内重读 + 补写快照）、
  `fork_from_snapshot` 的 I3 段
- 导演 prompt：`backend/agents/director_agent.py` 的 `_PLAN_PROMPT` / `_EVAL_PROMPT` 与解析函数
- **易漏的同步项**：
  - §5.4 三步：`models.py` → `repository._deserialize_*` 与快照的反序列化 → `frontend/src/types/index.ts`
  - `CLAUDE.md`：§3 代码地图、§4.1 模型表、§4.2 新增陷阱条目（继承边界 / 预算 / 契约 1）、
    §5.2 目录树加 `storyboard/`、§5.3 若新增进程内状态、§6.2/§6.3.1 链路、§8 API 表、
    §13 删掉"分镜稿"一行并升格到正文
  - 新增配置项（预算上限等）同步 `.env.example`
  - `NOTES.md#t18` 记录落地结论与 review 教训
