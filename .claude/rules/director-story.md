---
paths:
  - "backend/agents/director_agent.py"
  - "backend/services/orchestrator.py"
  - "backend/services/storyboard.py"
  - "backend/services/autopilot.py"
  - "backend/services/repository.py"
  - "backend/api/director.py"
  - "backend/api/branches.py"
  - "backend/scene_engine/engine.py"
  - "backend/scene_engine/termination.py"
---

# 导演、评估、决策、分镜稿与 AutoPilot（规则文件）

本文件管：场景收尾链、导演评估与主线度量、决策 `apply_decision`、分镜稿、AutoPilot、启动对账。
分叉与快照见 `fork-snapshot.md`；世界层（schema 2 / schema 1）见 `world-environment.md`；记忆固化水位线见 `memory.md`。

目录：
1. 场景收尾链（原 §6.2 第 6 步）
2. 决策 `apply_decision`（原 §6.3）与契约 5 细则
3. 启动对账（原 §6.4）
4. 陷阱 6 / 7 / 13 / 14 / 15 / 16 / 17 / 18（评估与主线度量）
5. 陷阱 21（分镜稿）
6. 陷阱 22（AutoPilot）

---

## 1. 场景收尾链（原 §6.2 第 6 步）

【实况】`orchestrator.run_scene` 在引擎返回之后依次做：

1. 落盘角色状态与 Scene（这是本场计数的**最后一次** `save_scene`）→ 推 `snapshot` 事件；
2. 记下导演此刻读到的分镜稿 `board_seen` 与目标版本 `seen_revision`（陷阱 21 的合并前提）→ 自动评估落库；
3. 补写后置快照的导演历史（`record_story_history`，失败只 warning + 非致命 `scene_error`）；
4. **世界补写按项目 schema 分流**：
   - schema 1：`_apply_world_delta` 在分支锁内重读世界变量、合并 `world_state_delta`、补写后置快照（细则见 `world-environment.md` 的 schema 1 兼容小节）；
   - schema 2：**不走这一步**（`unified is None` 才调）。环境事实已在场中随裁决事务提交；评估产出的 `world_operations` 只是"下一场前的提案"，**到 next_scene 决策时才提交**（见 §2），评估失败也不撤销已提交的环境事实；
5. 分镜稿 patch 逐条校验合并并补写后置快照（`_apply_storyboard_patch`，两种 schema 都走，与 schema 1 的世界补写共用同一把分支锁）；
6. 推 `evaluation` → 释放运行锁 → AutoPilot 决策（陷阱 22）→ 推终态帧 `completed` / `paused`（自动 continue 时不推）。

【契约】约束（动任何一条先想清楚）：

- **评估、补写各包独立 `try`**：这一场已跑完并打了后置快照，评估或补写的失败不得把状态打回 `paused`。决策 CAS 只接 `completed`，退回了用户就再也无法对这场决策。
- **推送 `evaluation` 之后不得再 `save_scene`**：用户收到评估就能决策，continue 会改写这一场，再整份覆盖 `data_json` 就把决策的改动抹掉。
- **补写窗口守卫 `_pending_world_patch`**：从引擎创建后置快照**之前**（`on_after_snapshot` 回调）到评估 + 补写全部结束（成败均可）为止，该快照 id 挂在集合里，期间 `fork_from_snapshot` 抛 409（详见 `fork-snapshot.md`）。标记必须早于快照可见；`finally` 必须罩住 `engine.run()` 本身，否则快照已建而引擎随后抛错时它永久不可分叉。schema 2 下世界事实无需补写，但**导演历史与分镜稿副本仍要补写**，所以窗口守卫对两种 schema 都有效。进程内状态，同属契约 9。
- 运行失败的兜底：`except` 里置 `paused` 落库（schema 2 下若已有世界提交落库，要以库里的场景为准，不能覆盖）。

## 2. 决策 `apply_decision`（原 §6.3）与契约 5 细则

人工提交与 AutoPilot 的自动决策走同一个函数（后者 `DirectorDecision.source = "auto"`，陷阱 22）。

- **continue**：`max_turns = turns_completed − 环境回合数 + extra`（默认 6），状态改回 `pending`，再起 `run_scene`（`start_continue=False` 时由 AutoPilot 自己开演）。**不写 decisions 表**（开启新一轮生命周期）。**不动 `inherited_story_history`**（陷阱 16）。已知边界：极晚到达的 continue 重试会再次续跑（确定性、不分叉，可接受）。
- **next_scene**：schema 2 项目先把本场评估的 `world_operations` 经 `commit_world_event`（事件 ID `transition:{scene_id}:{snapshot_id_after}`，幂等）提交，**continue 与 rollback 不应用**；再 `plan_scene(after_scene=被决策的那一场)`，应用人工覆盖，建新场景（记 `parent_scene_id`），写 decisions 表。
  - 目标恒为 `project.narrative_goal`；用户填的 `next_scene_description` 作为**本场意图**（`scene_intent`）单独传入。**不得用 `f"延续上一场…"` 冒充目标**：连跑几场后只剩动量、没有引力，`plot_deviation_score` 也就没了参照物（工单 28）。
  - 规划的前情与评估同源：沿 `after_scene` 的因果谱系走 `_story_context`，分叉处按冻结副本截止。**不要改回 `list_scenes(branch_id)`**，否则分叉出的新分支规划时看不到分叉点之前的剧情。
  - 物件覆盖（`next_objects_present`）先校验、非法 422，不该白付一次规划。
  - 所有"建新场景"的路径（手建 / `create_scene_from_config` / next_scene / 分叉）都要搬运 `speaker_mode`、`objects_present`、`environment_script`、`opening_narration`（陷阱 13）。
- **rollback**：回滚是条件为空的分叉，走 `orchestrator.fork_from_snapshot()`（`fork-snapshot.md`）；建 pending 的"（回滚重演）"场景，`snapshot_id_before=""`（契约 2）、`restore_snapshot_id=target`，**手工构造场景时漏传 `speaker_mode` 会让 selector 场景静默退回轮询**。目标快照缺失时**不持久化决策**，允许补 ID 重试。角色卡按快照态写回（仅展示缓存，权威态在快照与懒承接）。**不调 `restore_snapshot()`**。

### 契约 5 细则 — 幂等三件套（决策接口模板）

1. `decisions` 表（`scene_id` 主键 = 幂等键）持久化已生效决策 → 重试/重放返回同一 `next_scene_id`，不重复调 LLM、不重复建场景；提交不同 `decision_type` 抛 `ConflictError`(409)。
2. `scenes.status` 的 CAS 条件更新 `completed → deciding` 拦截并发，靠 SQLite 写锁跨进程有效；**只写列不写 `data_json`**。
3. `finally: clear_scene_deciding` 释放守卫（仅当仍为 `deciding` 时恢复 `completed`）。

任何可被用户重试 / 网络重放触发的写接口都要设计幂等键（分镜稿 PUT 用 `request_id`，物件用 `request_id`，AutoPilot 开启用 `request_id`）。

## 3. 启动对账（原 §6.4）

`main.py` lifespan 调两个函数：
- `reconcile_stale_builds()`：进度在 (0,1) 且既非完成也非失败的构建标记为失败，避免前端无限轮询；
- `reconcile_stale_scenes()`：残留的 `running` 场景改 `paused`。**只改状态，不自动重跑 LLM**——已产生的轮次都已逐轮落盘，由用户显式决定是否续跑。schema 2 的分支占用清理也在启动时做（见 `world-environment.md`）。

---

## 4. 评估与主线度量陷阱

### 陷阱 6 — `DirectorDecision.next_*` 覆盖字段
全为 `None` 才表示"保持 AI 自动规划"，不要用空字符串/空列表当默认值。

### 陷阱 7 — 四维评分语义
`narrative_goal` / `dramatic_tension` / `character_consistency` 越高越好；`plot_deviation` **越低越好**（0 = 完全贴合主线）。

### 陷阱 13 — `opening_narration` 等字段的权威载体是 `Scene`
`opening_narration` 的权威载体是 `Scene.initial_conditions`，`SceneConfig` 的同名字段只是规划期载体；`run_scene` 重建 `SceneConfig` 时从 `initial_conditions` 取值。**任何新增的建场景路径都必须显式搬运**（曾有 `create_scene_from_config` 漏搬，导演自写的开场白在 next_scene 路径永不生效，而手建路径正常——这类"两条路径只有一条对"的 bug 前端发现不了）。
`Scene.objects_present`、`environment_script` 同类：手建、`create_scene_from_config`、next_scene 人工覆盖、`fork_from_snapshot`（回滚也走它）四条路径都要搬，`tests/test_objects_present.py` 逐条钉住。写入侧校验存在性与上限（422），读取侧只去重截断、不核对物件是否还在（悬空 ID 由消费方跳过）。
详见 `docs/fix-tickets/04-director-context.md#t13-details`。

### 陷阱 14 — 分数为 `-1` = 评估未生成
`SceneEvaluation` 的四项分数为 `-1` 表示 LLM 返回无法解析为 JSON。不要当"很低的分"参与阈值比较（旧实现解析失败时给全部维度填 5.0，静默污染决策）。前端按负值显式提示；"失败的评估不得伪装成导演意见"。

### 陷阱 15 — `Project.narrative_goal` 是只读锚点
**只有用户能写**：写入口只有 `POST /projects` 与 `PATCH /projects/{id}`；导演侧任何路径都不得回写。自评系统的典型失效是把目标改成自己刚演出来的东西，然后分数变高。导演的可写空间是第二层——分支分镜稿（陷阱 21）。详见 `docs/fix-tickets/28-narrative-goal-and-ending.md`。

### 陷阱 16 — `story_progress` 的四重约束与导演历史冻结
动 `story_progress` 之前四条都要保持：

- **`PROGRESS_UNAVAILABLE`（-1）= "没度量到"**，绝不当"进度 0"参与钳制或停滞判定；它**不进** `is_evaluation_unavailable()`（那个函数语义是"整份评估作废"，漏返回一个键不该把决策打成保守默认）。
- **单调钳制**：落库值 `max(本场自评, 谱系历史最高)`，原始自评另存 `story_progress_raw`，未超过历史时置 `progress_stalled`（自评噪声大，不钳制进度条会抖）。
- **只在同一 `goal_revision` 内钳制**（`models.goal_revision()` = 目标的 sha256 前缀）：用户改目标就是换了尺子，不比对版本旧目标的 0.9 会把新目标进度永久钳到顶。旧记录 revision 为空串，与任何现行目标都不相等，不参与继承。
- **没有主线目标就不度量**（`SceneEvaluation.goal_missing`）：目标去空白后为空时推进度记 `PROGRESS_UNAVAILABLE`、不收自评，其余评分照留；"目标达成""主线偏离"没有参照——前端不判红，`make_decision` 的"目标达成 < 4 → 回滚"不生效（一致性规则照常）。评估 prompt 明确禁止把导演自拟的路线图当目标打分。字段上线前的记录按 `goal_revision == goal_revision("")` 回填（`repository._goal_missing`）。**不要把空目标的版本改成空串来省这个字段**：空串是"旧记录"标记，撞上就互相继承进度。

**导演历史按快照时点冻结**（`orchestrator._story_records`）——规则：

- `Snapshot.story_history` 保存当时已知的评估记录；前置快照不含本场，后置快照先带此前历史，自动评估完成后补入本场结果。`Scene.inherited_story_history`：None = 旧数据（回溯推断的**哨兵**），`[]` = 权威空历史。来源续跑、删除快照都不追写已有分支；分叉的 `parent_scene_id` 只管溯源，不据此回读来源最新评估；`evaluated_snapshot_id` 仅供追溯，继承边界只认快照的历史副本。
- 旧快照没有副本时 warning 并按空历史降级，不猜被覆盖的过去。手建场景在首次运行前冻结本分支前情。完整副本落库，提示词仍按既有梗概数量与 token 预算截断。
- **冻结副本与回溯之间必须去重**（`_merge_story_records`）：场景冻结进副本后被 continue 续跑并重新评估，再分叉时会两边各出现一次，回溯得到的那份更新故胜出。
- **continue 不得改写继承的过去**（`apply_decision` 刻意不动 `inherited_story_history`）：置 None 会把有权威边界的分支降格成旧数据，去读当前 `restore_snapshot_id`——来源快照已删则历史归零，来源后补评估则越界读入。本场评估无需清副本即可刷新（副本 `include_current=False`，回溯时从 `evaluations` 表现读，`save_evaluation` 是 `INSERT OR REPLACE`）。
- **`created_at` 必须在反序列化时还原**（`repository._deserialize_scene` 与 `SnapshotManager.get_snapshot` 都还原），否则"读出来改一改再存回去"的路径会把记录重排到时间线末尾。不要在调用点重读文件打补丁；损坏值降级 `now()` + warning，不抛异常。
- 副本是类型化的 `list[StoryRecord]`，反序列化统一走 `repository.deserialize_story_history`（场景与快照共用）：旧 `list[dict]` 当场读回；坏条目跳过并 warning；**整个容器不是列表按权威空历史 `[]` 处理，不是 `None`**（None 会触发回溯、越过分叉边界）；副本里无效的推进度（字符串 / NaN）降级为不可用。
- **回溯的查询次数与谱系长度无关**：先用 `repository.list_scene_lineage`（SQL 侧 `json_extract` 只取谱系字段，不读对白）走完因果链，再 `get_evaluations` 一次批量取回。`SceneLineage` 是只读投影，**拿去 `save_scene` 会抹掉整场对白**。

历史推演与旧实现的失效经过见 `docs/fix-tickets/28-narrative-goal-and-ending.md#t16-details` 与 `docs/fix-tickets/18-storyboard.md#t16-history`。

### 陷阱 17 — `unresolved_threads` 的合并由导演做
后端只去重截断。三处易错：
- LLM 未返回该键 → 沿用上一场列表，**不是**"线索全部收束了"；
- 谱系上**最近一份评估的空列表是权威值**，`_story_context` 不得因它为空就继续往前找（那会让已收束的旧线索复活）。空列表与"还没找到评估"靠 `StoryRecord.threads_known` 区分（旧副本缺键或非列表记 `False`，回溯继续往前找；随记录一起序列化，只看键在不在读出再存回就判错）。`_deserialize_evaluation` 把 evaluations 表缺键默认成 `[]` 是既有口径，未统一；
- 限条数（20）不等于限预算：`_normalize_threads` 同时限单条与总 token，**继承进来的列表也要过一遍**（库里可能有立预算之前写入的内容）。线索会落库并逐场回喂。

### 陷阱 18 — 数值解析必须挡住 NaN / Infinity / bool / 超大整数
`max(0.0, min(1.0, nan))` 返回**上界**，`json.loads` 默认接受裸 `NaN` / `Infinity`，`float(True)` 是 `1.0`——先钳后判会把 NaN 变成"满分"。统一走 `_parse_number()`（拒 bool + `math.isfinite`），捕获的异常里 **`OverflowError` 不可省**（超大整数 `float()` 抛的是它而非 `ValueError`，漏接会穿过 `_extract_json` 防线，废掉本可解析的评估）。

---

## 5. 陷阱 21 — 导演分镜稿 `Storyboard`

存放照搬 `WorldState`：`storyboard/{branch_id}.json`，快照带时点副本。实现是 `services/storyboard.py` 的纯函数。八条语义：

1. **只进导演 prompt**（契约 1）：它含导演对全部角色 `unknown_facts` 的安排。`SceneEngine` 拿到它**只为写前/后置快照的时点副本**，绝不并进 `scene_context`（陷阱 20）、角色 system prompt 或 selector 打分 prompt。
2. **节拍靠 `beat_id` 定位**（形如 `b7`，分支内唯一、只由后端分配）：改名/重排/改状态都不变；`next_beat_seq` 单调递增，**删除后的 ID 不复用**（否则基于旧稿的 patch 会命中同名新节拍）；ID 进 prompt 所以要短；分叉时沿用。
3. **导演的 patch 是相对它读到的那一版给出的**：锁内重读只挡得住整份覆盖，挡不住"导演读到 v1 → 用户改成 v2 → 导演基于 v1 覆盖用户"。`run_scene` 评估前记下 `board_seen`，`merge_storyboard_patch` 逐条核对前提：
   - memo 只在当前仍等于导演读到的那份时覆盖；
   - 导演没见过的 ID（`base` 里没有）一律当未知，哪怕当前稿里恰有同名 ID；
   - 重排必须是导演读到的全部计划中节拍的完整排列，且当前计划中节拍的**顺序**仍等于导演读到的（只比集合会让用户刚调好的顺序被旧稿无痕覆盖）；已是目标顺序按幂等处理；重排先于其余操作应用；
   - 被跳过的写 changelog + 非致命 `scene_error`，其余照常合并。用户↔导演、导演↔导演同一套规则。**不要改成"整份修订号不一致就丢整个 patch"**（同分支两场并发时，后完成那场改另一个节拍也会被整个丢掉）。
4. **`goal_revision` 的写回必须有确认**：只有导演 patch 里 `goal_realigned=true`（走 `_parse_bool`，`"false"` 不能变 True）或用户 PUT 带 `confirm_goal` 才前进；写回的是确认者**实际看到的**版本（导演是评估 prompt 里那版，用户是读取响应的 `current_goal_revision`，PUT 必须带回 `goal_revision_seen`，缺失 422）——期间目标又被改了的话，下一场照样提示过期。导演的确认是对它读到的那版路线图的判断，在预算淘汰**之后**判定：路线图被他人改过、本次有操作被跳过、或计划中节拍被淘汰，确认都不成立。**"被跳过"包括解析器因格式无效丢掉的操作**（`StoryboardPatch.rejected`：改写缺 `beat_id`、`reorder` 非列表等）——否则一个只剩 `goal_realigned` 的 patch 看起来就是"没改路线图、确认已适配"。值为 null 的键表示"不改"，不算格式错误。空 patch / 只改 memo / 解析失败都不算确认。空路线图没有"旧版目标"，首次产生节拍时盖上当次看到的版本。
5. **用户 PUT 的幂等键是 `request_id`**（契约 5）：与请求摘要一起记在那条用户 changelog 上，分支锁内查找——命中且摘要相同 → 重放（200、不写），摘要不同 → 422。"内容与当前相同"只是无操作判定，**代替不了幂等键**（带新节拍的请求首次保存后当前稿里已有分配的 ID，响应丢了的客户端拿不到，原样重发永远判不等，只会误报 409）。
6. **预算两道闸门**（同 `world-environment.md` schema 1 兼容小节的预算两闸门）：写入侧合并时超限淘汰（先淘汰最早了结的节拍，再从末尾淘汰计划中的，warning）、用户 PUT 超限 422 不截断；读取侧 `deserialize_storyboard` 调 `clamp_storyboard` 压回预算、ID 确定性补发，**只压不写回**。渲染时一切文本塌单行，备忘在存储中保留换行。
7. **继承边界**：`fork_from_snapshot` 把 `snap.storyboard` 写进新分支文件并附确定性模板生成的 `fork_origin`（分叉不调 LLM），排在 `fork_branch` 之前。旧快照没有副本（`None`）时以空稿起步并 warning，**不回读来源分支的当前分镜稿**（分叉之后才写的）；后置快照的副本在 `_apply_storyboard_patch` 里补写，同在 `_pending_world_patch` 窗口内。
8. **导演历史副本里不带 patch**（`_story_record` 清掉它，否则每场改稿复制 N 遍）。分镜稿也**不进** `make_decision` 的阈值规则，`unresolved_threads` 仍由评估逐场给出、不并入分镜稿。

分支临界区：与 schema 1 世界变量共用 `orchestrator._branch_lock`（按 `(project_id, branch_id)` 分桶，**只能有一把**）；落盘是"读-改-写"，临界区必须从重读开始，进程内锁同属契约 9。前端草稿/幂等/409 约束见 `frontend.md`（StoryboardPanel）。
详见 `docs/fix-tickets/18-storyboard.md`（含 `#t21-details` 推演）。

---

## 6. 陷阱 22 — AutoPilot（工单 12）

实现：纯函数在 `services/autopilot.py`（停止判定与记账），调度在 `orchestrator`。六条语义：

1. **会话只在进程内存**（`orchestrator._autopilot_sessions`，按**项目**登记——自动回滚会换分支）。不要挪进 `Project`：`PATCH /projects` 是整份读-改-写，逐步写计数会与用户编辑主线目标互相抹掉；落库后重启对账会让它无人值守地继续烧 LLM。重启即结束会话是刻意的。
2. **开启的"查重 → 校验 → 登记"必须在项目锁内**（`_autopilot_lock`）：否则同一 `request_id` 的并发重放各开一个会话、各开演一次；不同起点会两场都开演而项目只记得后登记的那个。**停止也在同一把锁内**：否则夹在开启的几次 await 之间到达的停止读到"还没有会话"就返回，会话随后照样登记，停止被静默吞掉。
3. **决策点在 `run_scene` 释放运行锁之后、发终态帧之前**（`_autopilot_after_scene`）：早于释放锁 → continue 的新 `run_scene` 被 `_active_scenes` 守卫静默丢掉；晚于终态帧 → 前端流收到 completed 就关了，无从得知下一场。**自动 continue 时不发终态帧**（迟到的 completed 会让重连客户端把新一轮判成已结束）。
4. **回滚上限按连续次数计、在执行前判**：不能按"回滚到同一快照"判（重演场景开跑时重打前置快照，目标 ID 每次都不同）。执行前判依赖 `make_decision` **不调 LLM**——先算出决策、过 `stop_reason_for_decision`，再作为 override 交给 `apply_decision`。**给 `make_decision` 加 LLM 调用之前先想清楚这条。**
5. **自动与人工决策走同一个 `apply_decision`**（CAS、幂等重放、回滚即分叉），只多 `source="auto"`。幂等重放命中人工决策、或 CAS 冲突，都按"有人接手了"停下。**开演权留在 AutoPilot**：它以 `start_continue=False` 调用，continue 只把场景改回 pending，由 `_autopilot_after_scene` 在 `is_running` 检查之后（两者之间不得有 await）自己开演，与 next_scene / rollback 同一条路——让 `apply_decision` 内部起续跑，决策期间的停止事后拦不住，会白烧一整轮。被停下的 continue 没有续跑，终态帧照常推。
6. **停止条件在无人值守下比界面更严**：评估不可用或评估之后的补写失败（`evaluated` 为 None）、抵达结局、`导演中断`（`termination.INTERRUPTED_REASON`；pause 走正常终止路径，场景照样 completed）都停。界面上结局只是提示、不锁按钮，两者不矛盾。

前端跟随语义见 `frontend.md`（autopilot 小节）。详见 `docs/fix-tickets/12-auto-pilot-director.md`。
