---
paths:
  - "backend/scene_engine/**"
  - "backend/agents/character_agent.py"
  - "backend/utils/turns.py"
  - "backend/services/orchestrator.py"
  - "backend/api/scenes.py"
  - "backend/api/schemas.py"
  - "backend/services/events.py"
---

# 场景引擎规则（运行一场戏 / 角色 prompt / 选人）

管：`SceneEngine` 的对话循环、角色 prompt 的 system/user 分层、selector 选人、场景运行的生命周期字段。
**不管**：固化/水位线/后置快照顺序（见 `memory.md` 陷阱 9）、收尾评估链与 AutoPilot（见 `director-story.md`）、
世界/环境/物件（见 `world-environment.md`）、LLM 出口与续写截断（见 `llm.md`）。

目录：运行场景前半段（原 §6.2.1–4）· 可恢复性 · 契约 2 细则 · 契约 3 细则 · selector ·
陷阱 1 / 2 / 3 / 4 / 8 / 20 / 24

---

## 运行场景前半段（原 §6.2 第 1–4 步）

1. `_active_scenes` 并发守卫：检查与写入之间**不得有 `await`**，靠单线程事件循环保证原子（契约 9）。
2. `_load_inherited_states` 取运行时记忆（契约 4）；开场读本分支世界（每次运行重读、**整场冻结**）与分镜稿。
   分镜稿**只交给引擎写进前/后置快照**，不进任何角色 / selector 上下文（契约 1，见 `director-story.md`）。
3. `build_character_agents`：**每场新建** `CharacterAgent` + `MemoryManager`（无跨场复用），`prime()` 回填短期缓冲与事件摘要；
   长期记忆靠 ChromaDB 目录天然连续。`branch_id` 必须传对（`memory.md` 陷阱 11）。
4. `SceneEngine.run(on_turn=...)` 单轮顺序：
   前置快照（契约 2）→ `check_termination` → `_select_speaker` → `agent.respond()` →
   截掉续写（`trim_continuation`，截完为空再要一次，见 `llm.md` 陷阱 28）→ `_split_reply` / `_parse_turn` 拆
   `*动作*` / `[独白]` / 对白 → （record / adjudicate 档）动作意图抽取（早于第一次落盘，见 `world-environment.md` 陷阱 25）
   → 追加 transcript → 对本场**全部参演角色**调 `add_experience`（在场即记忆，工单15）→ 周期固化 → SSE 推送
   → （adjudicate 档）逐个裁决本轮 pending 动作（见 `world-environment.md` 陷阱 27）。
   - 固化的触发权只在引擎，固化 / 水位线 / 落盘先于 SSE 推送、收尾固化先于后置快照：**全部细则见 `memory.md` 陷阱 9**，
     动这一段前必读，此处不复述。
   - 每个角色能感知什么由记忆层按 `utils/turns.perceive` 自行判定（他人轮次剥离 `inner_thought`），调用方不传判定。
   - **解析必须先剥独白、再取动作**（契约 1 细则，`_split_reply` / `_parse_turn`）；动作段与 `turn.action` 同源。

**`SceneEngine` 不碰 SQLite**：结果写回传入的 `Scene` 对象，由 orchestrator 落盘（契约 8）。
引擎也不自建 LLM 客户端；环境裁决器由编排层注入。

## 运行中的可恢复性（工单23）

开跑前先把 `status=running` 落库；引擎每产生一轮就先把 `dialogue_log` / `turns_completed` 写回 `scene` 对象，
再回调 `on_turn`，由 orchestrator **逐轮 `save_scene`** 后才推 SSE。故刷新 / 断线 / 进程退出都能从 `GET /scenes/{id}` 拿回已产生轮次；
运行失败置 `paused` 并落库（否则永远卡在 running）。**不要为"减少写入"改回结束时一次性保存。**

## 契约 2 细则 — 快照前置（含唯一例外）

场景模拟必须有前置快照；**唯一例外**：`scene.snapshot_id_before` 非空（continue 续跑）时引擎刻意跳过创建，避免覆盖首次快照。
这是特性不是 bug（陷阱 1、陷阱 8）。

## 契约 3 细则 — Prompt 前缀稳定（prefix cache）

- `system` 只放**整场不变**的内容：人设 / 世界观 / 已知事实 / 关系 / 场景设定 / 格式规范。
  每轮变化的（目前对话、检索到的记忆、发言指令、**当前环境**）一律放 `user`。
- "目前对话"**只在末尾追加、不做逐行滑窗**；超出 `TRANSCRIPT_TOKEN_BUDGET` 时**成块丢到 75% 水位**并记录 `_transcript_start`，
  之后沿用同一起点（`CharacterAgent._recent_transcript`）。`RECENT_TRANSCRIPT_WINDOW`（默认 0 = 不启用）是行数上限，
  同样只前推起点、不逐行滑动。
- user 内顺序固定：目前对话 → 【当前环境】 → 想起的记忆 → 发言指令。环境块**必须排在对话之后**：对话只追加，
  放前面则每次裁决都改前缀。
- 目的：同一角色同一场景内 prompt 前缀稳定、命中 prefix cache。**任何"优化"都不得把变化内容塞回 system，也不得改成逐行滑窗。**
- "什么动态世界数据走 user、什么冻结后进 system"的分层表（场景常量 vs 场景内变量、schema 1 vs 2）由
  `world-environment.md` 的契约 3 补充条款负责，这里不重复。

## selector 选人（工单11）

- `selector` 模式 = `ScoringSpeakerSelector`：每个候选各发一次**独立**并行打分调用（一次只看一个角色，避免列表位置偏见），
  再叠加被点名加分（只回看最近 2 轮）与重复发言惩罚，取 argmax（`SELECTOR_TEMPERATURE>0` 时 softmax 采样）。
- **契约 1**：打分 prompt 只许用角色 `known_facts` 与公共对话文本，**绝不含 `unknown_facts`、绝不含任何 `inner_thought`**
  （transcript 由 `_turn_line` 生成，已剥独白）。【当前环境】经 `environments` 参数按角色的可见投影传入，不得传导演全量视图。
- **契约 3**：每个角色的打分 system 前缀整场不变，对话只追加、超预算成块丢弃，与角色 prompt 同构。
- 兜底必须 **warning 可见**，不得静默选 `agents[0]`；降级时 `DialogueTurn.selector_notice` 写一句灰字（陷阱 4）。
- 选人只数**角色轮次**（`utils/turns.character_turns`），轮询按角色轮次取模，不按全局 `turn_number`（陷阱 24）。
- selector 可挂独立 `LLM_SELECTOR_BASE_URL / API_KEY`，见 `llm.md`。

---

### 陷阱 1 — `Scene.snapshot_id_before` 为空是有意义的信号

`SceneEngine.run()` 只有在它为空时才创建前置快照。回滚重演场景**必须**留空它，否则跳过快照、丢掉新初始条件。

### 陷阱 2 — `Scene.restore_snapshot_id` 与上者分工

前者管"从哪儿回填运行时记忆"，后者管"要不要打快照"。两者不可合并。

### 陷阱 3 — `speaker_mode` 是一条贯通的链

`Scene.speaker_mode` 与 `SceneConfig.speaker_mode` 同一条链：缺省来自 `settings.DEFAULT_SPEAKER_MODE`，由 `CreateSceneRequest`
或 `DirectorAgent.plan_scene` 写入 `Scene`，再由 `run_scene` 传给 `SceneConfig`。
**所有新建 `Scene` 的地方都要跟**——尤其是 `apply_decision` 的 rollback 分支（手工构造重演场景，漏传会让 selector 场景静默退回轮询）。
取值有两处校验（`CreateSceneRequest` 422、`Settings.DEFAULT_SPEAKER_MODE` 启动即失败），
`SceneEngine._select_speaker` 兜底回退 round_robin 时 warning 一次。
（工单11 打通经过见 `docs/fix-tickets/11-selector-and-world-interaction.md#trap3-history`）

### 陷阱 4 — `DialogueTurn.selector_notice` 只是展示信号

只在 selector 选人降级时非空（服务不可用 / 部分打分失败），给前端在角色名后显示一句灰字。
**不参与任何逻辑判断，也不进入角色 prompt。** 同类的 `output_notice` 见 `llm.md` 陷阱 28。

### 陷阱 8 — `SceneStatus.PAUSED` = "跑一半断了"

运行异常、服务重启对账都会落到此状态。它**不可提交决策**（CAS 只接 completed），但可再次 `POST /scenes/{id}/start` 续跑：
`snapshot_id_before` 已存在 → 不重打快照；`dialogue_log` 已逐轮落盘 → 注入历史续接（水位线之后的重放见 `memory.md` 陷阱 9）。
（`pause` 接口走的是正常终止路径，场景最终是 `completed`，与此状态无关，见 `docs/` 已知缺陷。）

### 陷阱 20 — `scene_context` 里的非成句键会逐条渲染进角色 **system**

`CharacterAgent._scene_brief` 把 `name / location / description / opening_narration`（= `RESERVED_SCENE_CONTEXT_KEYS`，成句）之外的
键逐条渲染成 system 里的"当前情境"清单。因此**往 `scene_context` 塞任何键都等于公开给本场全部角色**，内部记账字段不要走这个 dict。
`_BRIEF_KEYS` 必须与 `RESERVED_SCENE_CONTEXT_KEYS` 是同一个常量对象（两边漂移会出现"拦住了却不成句"或"没拦住又被顶掉"）。

适用范围（按代码 `SceneEngine._scene_context`）：

| 项目 | `scene_context` 的构成 | 世界层进 system 吗 |
|------|-----------------------|-------------------|
| schema 2（默认） | 四个固有键 + `config.initial_conditions`（`self.world_variables` 被置为 `{}`） | **不进**。动态世界走 user 的【当前环境】可见投影（`_respond_extras` → `render_world_for_character`） |
| schema 1（旧项目） | 上述 + 垫在底下的分支世界变量（`{**世界变量, **initial_conditions}`） | 进（整场冻结，契约 3 补充条款，见 `world-environment.md`） |

schema 2 下 `initial_conditions` 仍会进 system 的"当前情境"，所以它只许承载**本场公开提示**，事实修改走世界命令；
与世界属性同名的条件在引擎入口被移除（只在运行副本上移，不落库，否则分叉 I5 与续跑会永久丢条件）。
这条保护的是 system 前缀，不是世界隔离。

### 陷阱 24 — 轮次的渲染、感知、计数各只有一处实现（`utils/turns.py`）

- **计数分两种**：`turn_number` / `turns_completed` / 水位线 `turns_consolidated` 数**全部**轮次；`max_turns`、停滞检测、
  轮询选人、continue 的新上限只数**角色轮次**（`character_turns`）。continue 从 `turns_completed` 里扣掉环境回合数，而不是改数日志。
  新增按轮次计数的逻辑先想清楚是哪一种，不要直接 `len(dialogue_log)`。
- **感知只经 `perceive(turn, viewer_id)`**：`add_experience` 与 `EpisodicMemory` 用自己的 `character_id` 判定，不接受调用方传布尔
  （现场写入与续跑重放各判一次会漂移，而事件摘要靠两边逐字相同去重）。
- **`render_turn` 不去首尾空白**：记忆文本是长期记忆的寻址键，改一个字符就是新记录；"目前对话"由引擎自己 strip。
- **`EpisodicMemory._snippet` 的条目格式冻结、不并入 `render_turn`**：它是序列化格式，老快照与重放去重都靠逐字相同。
