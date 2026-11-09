---
paths:
  - "backend/services/world.py"
  - "backend/services/world_access.py"
  - "backend/services/world_repository.py"
  - "backend/services/world_state.py"
  - "backend/services/environment.py"
  - "backend/services/objects.py"
  - "backend/services/orchestrator.py"
  - "backend/services/repository.py"
  - "backend/agents/environment_agent.py"
  - "backend/agents/character_agent.py"
  - "backend/scene_engine/engine.py"
  - "backend/scene_engine/action_intents.py"
  - "backend/scene_engine/speaker_selector.py"
  - "backend/api/branches.py"
  - "backend/api/objects.py"
  - "frontend/src/components/WorldPanel.vue"
  - "frontend/src/components/ObjectsPanel.vue"
---

# 世界与环境层规则（统一世界 schema 2 / 旧世界变量 schema 1 / 物件 / 环境裁决）

本文件管：分支级世界状态怎么存、谁能看见、怎么提交、怎么迁移；物件与环境裁决（环境回合）；动作意图；
以及"动态世界数据放 system 还是 user"的分层（契约 3 补充条款）。
改世界 / 物件 / 环境相关文件，或改 `character_agent` / `engine` / `orchestrator` 里把世界内容送进 prompt 的地方之前读这里。
**写任何规则前先问：这条对 schema 1、schema 2 还是两者成立？** 两套规则不得混写成一句无条件的话。

目录：两套 schema 一览 → schema 2（原 §4.1.1）→ 契约 1 细则（世界/环境部分）→ 契约 3 补充条款 →
陷阱 25（动作意图）→ 陷阱 27（环境回合）→ schema 1 兼容（旧项目，含陷阱 19、陷阱 20 的 schema 1 部分）→ 环境智能体现状（原 §13）。

相关：陷阱 20（`scene_context` 逐条进 system）、陷阱 24（轮次计数与感知）在 `scene-engine.md`；分叉 / 快照窗口在 `fork-snapshot.md`；
分镜稿在 `director-story.md`；迁移后的存储布局在 `persistence.md`。

---

## 两套 schema 一览

`Project.world_schema_version`：`POST /projects` 新建的项目为 **2**；缺该字段的旧项目按 **1** 读取（`models.py`，`repository.py`）。
每个项目只有一条权威写入路径，**不双写**。

| 维度 | schema 2（默认） | schema 1（旧项目） |
|------|-----------------|-------------------|
| 世界载体 | `WorldRevision`（不可变版本，SQLite `world_revisions`；`world_heads` 记分支当前版本） | `WorldState`（文件 `world_state/{branch_id}.json`，`dict[str,str]`） |
| 谁能看见 | 限知：`global` / `observable`（需在场且能感知）/ `hidden`，经可见投影 | 全公开：全部变量进本场每个角色的 system |
| 注入位置 | **user** 的【当前环境】（逐角色投影）；system 不再摊入世界变量 | **system** 的"当前情境"（整场冻结） |
| 场内变化 | 环境裁决经事务提交成新版本 | `Scene.environment_state`（仅本场有效，adjudicate 档） |
| 场间演进 | 导演 `world_operations` 提案，next_scene 时提交；用户经命令接口 | 导演 `world_state_delta` 场末补写 |
| 快照 | 前/后置快照记 `world_revision_id`，无需补写 | 快照带 `world_state_variables` 副本，后置快照要补写 |

---

## schema 2：统一世界与环境（原 §4.1.1）

**【实况】** 代码：`services/world.py`（无 IO 的领域规则）、`world_repository.py`（持久化，经 `repository` 暴露）、
`world_access.py`（用户命令、规则采用、迁移）。没有新增依赖。

### 数据与定位

- 一项事实由 `(subject_kind, subject_id, attribute)` 定位（`world.fact_key`），`subject_kind ∈ {world, object, location}`。
  值保留 bool / 数字 / 文本 / null / 实体引用（`{"kind": character|object|location, "id": ...}`）类型。
  **显示名称不承担身份**。`set` 与 `unset` 明确区分；删除保留时点标记（`removed_facts`），使角色重新到场能观察到"属性已清除"。
- 当前地点属性的现场匹配用 `Scene.location` 标识；地点尚无独立注册表。
- `WorldRevision` 保存完整属性、各角色的历史观察（`observations`）和物件定义（含隐藏规则、可见性）。
  **版本不可修改**：分叉可引用同一版本，后续写入各自生成新版本。完整版本占用较多存储，**当前不做版本回收**。
- `initial_conditions` 只承载本场提示，**事实修改走世界命令**。与可见世界属性的标识或标签同名的条件，引擎在入口**移除并 warning**
  （不抛错：抛错会让 pending 场景永远开不了；只在运行副本上移除、不落库，否则分叉 I5 与续跑永久丢条件；`engine.py`）。

### 可见性与观察（契约 1 在 schema 2 的落点）

- `global`：明确允许广泛传播；`observable`：需要在场且能感知对应物件 / 地点；`hidden`：不直接提供给角色，不向观察者分发。
- **知识是观察，不是订阅**：曾经获知某物件状态只留下当时的观察（渲染为"此前获知（不代表当前状态）"），不跟随后来的值；
  重新进入现场时更新观察。`global` 更新会使所有人对该键的旧观察作废（含删除）。
- 在场者目睹 private 物件的 observable 变化即视为**发现**了它（公开叙述本就进全场 transcript，观察必须与叙述一致）；
  不想让旁人发现的变化应由裁决器写成 `hidden`。
- **个人推测和言论不提升成客观世界属性**；角色记忆（`memory.md`）继续独立工作。
- 提示词的展示预算（`render_world_for_character` 的 `budget_tokens`）**只省略显示行，不删除事实**；完整数据看世界面板和事件接口。

### 提交与事务

- 世界版本、事件、场景环境回合、动作状态及轮次 `revision` **在同一个 SQLite 事务提交**（`world_repository.commit_world_event`，
  同事务里 `UPDATE scenes SET data_json`）。**提交完成后才写记忆、推 SSE。**
- 幂等键是 `event_id`（与请求摘要比对：同键异内容 409）；`expected_revision_id` 不匹配 409。契约 5 的 schema 2 实例。
- 环境裁决结果失败的两种语义（`engine.py`）：`InvalidRequestError`（准入失败，如引用了不存在的实体）→ 动作 `failed`、不重试；
  其他异常（IO 失败）→ 动作回到 `pending` 并向上抛（场景转 paused），恢复时可补裁决。**不要把后者也记成 failed。**
- **裁决输出不因 prompt 预算截断；非法结果整次拒绝**，不保留与未接受变化对应的叙述（`apply_operations` 先整体校验再采纳叙述）。
- 环境来源的 operation 只许 `subject_kind=object`、目标在本次允许集合内、`visibility ∈ {observable, hidden}`；
  导演来源只许 `subject_kind=world` 且 `visibility=global`（`world.apply_operations`）。

### 运行占用

每个 schema 2 分支**同时只有一个场景推进世界**：`world_heads.running_scene_id` + 执行 token（`acquire_world_execution`）。
同分支其他场景、外部编辑（命令接口、规则采用）被拒（409）；不同分支可并行。启动时在契约 9 的单进程前提下清理遗留占用，
**不声称支持多 worker**。场景起点之后分支世界已推进（`scene.world_revision_id != 当前头`）→ 拒绝开演，需从该场快照分叉后继续
（`run_scene` 开头）；有对白却没有 `world_revision_id` 的旧场景同样拒绝，需从历史快照分叉。运行初始化与迁移共用项目锁（`__world_schema__`）。

### 环境裁决与脚本

- 裁决器（`EnvironmentAgent.adjudicate_world`）读版本内规则 + 本次允许作用物件的状态 + **世界层 `global` 事实**（判断可行性，如"宫门封锁中"）；
  **hidden / observable 的世界事实不进裁决器**。当前作用范围仍是**目标物件**；主动环境事件与通用多物件物理模拟尚未实现。
- 场景脚本 `Scene.environment_script`（`{object_id: [RevealEntry]}`）经规划、手动创建、next_scene、分叉传递，**仅环境侧读取**。
  预制揭示在裁决 prompt 里只渲染触发条件（`condition`），裁决器只回答命中第几条（`reveal_index`），**内容**由代码原样交给执行者。
- 现场生成揭示是**独立的第二次调用**：只拿当事人的 `known_facts` 与可用记忆，**拿不到隐藏规则、预制内容、他人信息**；
  触发只接受固定代码 `actor_memory`，不转交裁决器自由书写的效果说明；生成失败保留有效的公开裁决。
- 裁决前会检索一次当事人记忆（一次 embedding 调用），只有触发揭示时才用得上——已知的浪费，不要顺手优化掉隔离。

### 导演提案

评估的 `world_operations` 是**下一场开始前的宏观提案**，只许改世界层广泛公开属性。`apply_decision` 的 next_scene 分支经同一事务入口提交
（`orchestrator.py`）；**continue 与 rollback 不应用**。评估失败不撤销已提交的环境事实；schema 2 下评估后**不再有世界补写**
（`unified is None` 才调 `_apply_world_delta`，`orchestrator.py`），但分镜稿补写与 `_pending_world_patch` 窗口照旧（`fork-snapshot.md`）。
**已知缺口**：导演提案只能写 global 事实，无定向通告；提案自动提交、界面只读，导演仍可能把认知类事实写成 global
（`docs/fix-tickets/schema2-refinements.md` P2-4，AutoPilot 下无人查看）。

### 快照与分叉

- 前/后置快照的 `world_revision_id` 分别引用实际起点和场末事实；物件规则随版本走。
  编辑项目物件卡后，需要在某分支 `adopt-definitions`（"采用当前物件规则"），旧快照保持旧规则。
- schema 2 项目从**没有** `world_revision_id` 的旧快照分叉：不猜历史物件状态，新分支从空世界起步，
  快照里的旧变量记为 `legacy_records`，并采用项目当前物件定义（否则无从裁决）；不回读来源分支的当前世界（`orchestrator.py`）。
- `fork_from_snapshot` 的 I3 在 schema 2 下是 `initialize_branch_world(from_revision_id=…)`（引用同一不可变版本，不拷贝）。

### 迁移（schema 1 → 2，显式、不可逆）

- `GET /projects/{pid}/world/migration-preview` 只生成分类与内容指纹（`preview_revision`）；
  `POST /projects/{pid}/world/migrate` 接受显式属性选择，**事务内切换全项目所有分支和 schema**；预览后旧数据变化 → 409。
- 未选择的原始记录完整保留为 `legacy_records`，**不向角色广播**；不改旧文件和快照，也不重写角色记忆。
- 运行 / 决策中的项目不能迁移。旧场景已有对白的不在新体系续跑，需从历史快照分叉。

### 接口（均挂在 `/projects/{pid}` 下，完整表见 `api.md`）

`GET /branches/{bid}/world-state`（按 schema 返回旧变量或完整新版本）· `POST /branches/{bid}/world/commands`
（`request_id` / `revision_id` / `operations` / `observers`）· `POST /branches/{bid}/world/adopt-definitions` ·
`GET /branches/{bid}/world/events`（含分叉起点前的继承来源，**不含来源分支之后的变化**）· 迁移两个接口。

验收：`tests/test_unified_world.py`、`tests/test_unified_world_api.py`、前端 `tests/world-panel.test.mjs`
（状态/观察分离、类型与删除、事务/幂等/恢复、规则与快照分叉、评估失败、场间提交、显式迁移、切分支与迟到响应隔离）。
设计理由与后续修正见 `docs/fix-tickets/schema2-refinements.md#schema2-design-notes`、`docs/fix-tickets/24-20-environment.md#trap25-27-reasoning`。

---

## 契约 1 细则（世界 / 环境部分）

- **该隔离的**：物件 `hidden_rules`、预制揭示的 `content`、`unknown_facts`、导演分镜稿——都**只进导演与环境裁决层**。
  角色与 selector 的【当前环境】一律经**可见投影**（`render_world_for_character`），不得传全量版本。
- **公开 ≠ 全局**：schema 2 里"现场公开"（本场 transcript、公开叙述）不等于跨场广播；能跨场传播的只有 `global` 事实与各角色自己的观察。
- **裁决结果天然带私密性**（"只有他看见水盆亮了"）：公开叙述进全场 transcript 与全体在场角色记忆；私密细节只进执行者（`perceived_by`）。
  任何让裁决结果沉淀为世界事实的路径，都必须先过 visibility 判据（`observable` 还是 `hidden`）。
- **schema 1 专属**：`WorldState` 只允许存放所有角色都可感知的公开事实（季节、势力公开态度、公开事件）——它会进每个在场角色的 system，
  放进只有部分角色知道的秘密就是一次性向全员泄密。这条约束靠导演评估 prompt，后端无法代为判断。schema 2 不依赖这条：由 visibility 字段结构性承担。
- 解析先剥独白再取动作、`inner_thought` 不进他人 prompt 等见 `scene-engine.md` / 契约 1 总述；动作意图抽取只用剥过独白的动作段（陷阱 25）。

---

## 契约 3 补充条款：动态世界数据放哪

`system` 只放整场不变的内容，变化内容放 `user`（总则见 `scene-engine.md`）。世界 / 环境内容按 schema 分层：

| 内容 | 何时变 | schema 2 | schema 1 |
|------|--------|----------|----------|
| 场景固有键、`initial_conditions` | 场次之间 | system "当前情境" | 同左 |
| 世界变量 / 世界事实 | 场次之间（schema 2 另有场内裁决） | **user 【当前环境】**（逐角色可见投影） | **system "当前情境"**（垫在 `initial_conditions` 之下，整场冻结） |
| 场景内物件状态 | 场内由裁决改变 | 同上（就是世界事实，不再有单独的场内状态） | **user 【当前环境】**（`Scene.environment_state`，仅 adjudicate 档） |
| 【在场物件】（名称 + 公开描述，按可见性） | 整场不变 | system | system（仅 adjudicate 档） |

- user 内顺序固定：目前对话 → 【当前环境】 → 想起的记忆 → 发言指令。环境块排在对话之后，否则每次裁决都击穿前缀缓存。
- 把场内变化的状态塞进 system 会**每轮击穿 prefix cache**（工单11 §2.2 曾设想过，已排除）。
- schema 2 下 `engine.world_variables` 被置为 `{}`（`engine.py`），`_scene_context` 里没有世界层内容。
  不要为了"补全"把世界事实摊回 `scene_context`（陷阱 20：进那个 dict 就公开给本场全员）。
- selector 的打分 prompt 同样经 `environments` 参数按角色投影传入，不得传导演全量视图。

---

## 陷阱 25 — `DialogueTurn.actions`（动作意图）

`ENVIRONMENT_MODE` 为 record / adjudicate 时才产生（`config.py`，默认 `off`）。**两套 schema 通用**，除另注。

1. **不进任何角色记忆、transcript 或 prompt**：`render_turn` / `_turn_line` / 记忆文本只读 `action` 拼接字段。
   这是**设计保证**，`test_record_mode_leaves_memory_and_transcript_byte_identical` 逐字比对 off 与 record 两档。
   环境结果要进角色视野走**环境回合**（陷阱 27），**不要**把 `detail` 渲染进 `_turn_line`。
2. **抽取 prompt 只有命中段**（不传对白、不传隐藏规则），段内 `[...]` 先剥掉（`action_intents.strip_thoughts`，没闭合的 `[` 剥到段尾）：
   `text` / `detail` 会交给裁决器产出公开叙述，独白混进去就是泄露。
3. **动作段与 `turn.action` 同源**（`SceneEngine._split_reply`，先剥独白再取动作）：抽取**不得**拿原始回复重跑动作正则，
   否则独白里的 `*戴上王冠*`（想做、没做）会被当成尝试，adjudicate 档把角色心里的打算裁决成全场可见的环境回合。
4. **抽取先于第一次落盘**：落盘的轮次意图恒完整，续跑不重抽；命中段超过 6 段按段落顺序取前 6，结果按 `index` 回填——结果可复现是"续跑不重抽"的前提。
5. **`ENVIRONMENT_MODE` 按段冻结**：每次进 `run_scene` 读一次；continue 重进会重读，**同一场的前后两段可处于不同档位，`actions` 为空不代表没有物件动作**。
6. **读取侧收紧**（`repository._deserialize_actions`）：未知 `status` 一律按 `skipped`（续跑会按 `pending` 补裁决，脏值不能变成待裁决）；
   在场物件悬空 ID 跳过时 warning——静默跳过会让 record 档命中率无声归零。

---

## 陷阱 27 — 环境回合（`ENVIRONMENT_MODE=adjudicate`）

环境回合 = `TurnKind.ENVIRONMENT` 的 `DialogueTurn`（`narration` 公开叙述 + `private_detail` + `perceived_by` + 来源轮次 / 动作序号 + `revision`；
`turn_id` 由 `(来源轮次, 动作序号)` 确定性生成 `environment_turn_id`）。默认档位 `off`；设计判据见 `docs/fix-tickets/24-20-environment.md` §5 / §9。
以下每条标明适用范围。

1. **一个动作一个提交单元**（两套通用）：角色轮次推送之后、下一次终止判定之前，`SceneEngine._adjudicate_pending` 逐个裁决本轮 pending 动作。
   成功 = 追加环境回合 + 动作 `resolved` + 来源轮次 `revision+1`；**落盘先于推送**，且先推 `turn_update`（来源轮次）再推 `turn`（环境回合）。
   环境回合作为普通轮次走 `_remember` 与周期固化（`memory.md` 陷阱 9），**不另开记忆写入口**。
   - schema 1：`Scene.environment_state` 一并更新，`on_environment` 一次 `save_scene` 提交；
   - schema 2：世界新版本 + 事件 + 场景 `data_json` 在 `commit_world_event` 的同一事务里提交（`_on_environment` 不再重复 `save_scene`）；
     `_remember` 在提交**之后**。
2. **失败不伪造**（两套通用）：裁决调用失败或输出不可用 → 动作 `failed`、不生成环境回合、推非致命 `scene_error`。**绝不补一句"毫无反应"**（同陷阱 14）。
   "裁决成功但没触发规则"照样生成环境回合，那是裁决出的事实。（schema 2 的 IO 失败例外见上"提交与事务"。）
3. **额度在调用前预占**（`remaining_environment_quota`，上限 `MAX_ENVIRONMENT_TURNS`）：上限 − 已有环境回合 − 仍 pending 的动作。
   ≤0 时命中段直接 `skipped/quota`、**不发起抽取**；pending 必须计入，否则续跑补裁决后超限。
4. **续跑先补裁决**：`_replay_unconsolidated` 之后按日志顺序补 pending 动作；中断只在动作之间生效，剩下的留 pending。
   已有同 ID 环境回合的动作改记 `resolved`；物件已不在本场记 `failed`；**非 adjudicate 档原样保留 `pending`、仍占额度**——改成 `skipped` 就不可恢复了。
5. **可见性三条线**（两套通用，契约 1）：公开叙述 `narration` 进"目前对话"与全体在场角色记忆；`private_detail` 只进 `perceived_by`
   （固定为执行者本人）的记忆与导演 / 用户界面，**不进 transcript**；物件隐藏规则与预制揭示 `content` **只进裁决 prompt**
   （`test_hidden_rules_reach_only_the_adjudicator` 扫遍角色 / selector / 抽取的全部 prompt）。
   私密细节只能是导演预制揭示的原文，或 schema 2 下"仅凭当事人已知材料"的独立生成调用；**裁决器自己在裁决里写的不采纳**。
6. **角色侧的变化**：
   - system 里的"动作只写尝试"规范（`attempt_only`）**只在 adjudicate 档**出现；
   - 【在场物件】（按可见性，只有名称与公开描述，总预算 600 token）：**schema 1 只在 adjudicate 档**；
     **schema 2 只要场景有可见的在场物件就出现，与档位无关**（见 `engine.py` 的 `_respond_extras`）。
   - 因此"off / record 档角色 prompt 逐字不变"的 golden 用例（`test_off_and_record_character_prompts_are_byte_identical_to_before`）**只对 schema 1 成立**，
     不要据此推断 schema 2。【当前环境】在 schema 2 下也不限档位，有可见观察即出现。
7. **物件状态的归属**：
   - schema 2：物件状态就是世界事实 `(object, object_id, attribute)`，**按 `object_id` 归属、按分支版本跨场延续、按可见性投影**；
     `Scene.environment_state` 在 schema 2 **不使用**（见 `models.py` 的 `Scene.environment_state` 注释）。跨场泄露（private / hidden 物件状态摆到不知情者眼前）由可见性投影结构性避免。
   - schema 1：`Scene.environment_state` 形如 `{object_id: {属性: 值}}`，**只在本场有效**，渲染时才按物件当前名字拼 `物件名·属性`；
     裁决器只读本场状态（`object_state_view`）。**不要**把它并进 `WorldState`：世界变量进全体角色的 system，private / hidden 物件的状态会泄露给不知道它存在的人。
   - 物件之间名称与别名不得重名（casefold；新建 / 编辑 422）。

物件数据的边界：`WorldObject` 项目级、文件 `objects/{object_id}.json`、可人工编辑，读取侧要压回预算（`objects.clamp_object`）；
每项目上限 40，每场在场物件上限 10（超了 422，读取侧只去重截断）。`Scene.objects_present` 四条建场景路径都要搬运（陷阱 13，见 `director-story.md`）。
可见性取值非法按 `hidden`（失败即收紧，同陷阱 23）。

---

## schema 1 兼容（旧项目）

以下规则**只适用于 `world_schema_version == 1`**。schema 2 项目里这些代码路径不会走到（或已被取代）。

### 陷阱 19 — `WorldState` 的不可分割语义（工单07，schema 1 专用）

`WorldState` 是分支级、整场冻结的公开世界变量，进每个在场角色的 system。保持以下约束，少一条就会静默退化
（推演、案例与评审时间线见 `docs/fix-tickets/07-world-state.md#trap19-reasoning`）：

1. **合并只发生在运行时**：`SceneEngine._scene_context()` 按 `{**世界变量, **initial_conditions}` 合并（场景局部覆盖全局），
   **绝不写回 `Scene.initial_conditions`**——写回落库会让分叉 I5 把世界快照当场景局部条件永久带下去，之后同名变量再也改不动。
2. **四个场景固有键是保留字**（`models.RESERVED_SCENE_CONTEXT_KEYS` = name / location / description / opening_narration）：
   世界变量只许补充场景上下文，不许改写场景是什么。三处拦截（`normalize_world_delta` / `merge_world_variables` / `SceneEngine._reject_reserved`），
   且名单必须与 `CharacterAgent._BRIEF_KEYS` 是**同一个常量**。
3. **预算是硬约束，写入与读取各一道闸门，读取侧不可省**：`merge_world_variables` 限条数（`MAX_WORLD_VARIABLES`）与总 token，
   超限淘汰**最久未更新**的键并 warning；`repository._deserialize_world_state` 读文件时调 `clamp_world_variables` 压回同一形状与预算
   （文件明确支持人工编辑，会绕过写入侧）。**键和值都要塌成单行**（渲染按"一行一条"，键的换行同样能凭空多出一条）。
   **只压不写回**：读路径不得因一次读取改掉用户手编的文件。这三个函数与 `describe_world_state` 住在 `world_state.py` 仅为避免 import 成环。
   （分镜稿的预算两闸门同构，见 `director-story.md`。）
4. **`delta` 里 value 为 `None`（或空串）= 删除该变量**，是唯一的收束手段；评估解析失败时 `world_state_delta` 必须为空 dict，
   绝不能让一次失败的 LLM 调用伪装成真实的世界更新。
5. **后置快照要补写**（`record_world_state`）：delta 出自评估，而后置快照在评估之前就打好了，不补写则从该快照分叉的分支缺本场改动。
6. **落盘是"读-改-写"，临界区必须从重读开始**（`orchestrator._branch_lock`，按 `(project_id, branch_id)` 分桶，**与分镜稿共用同一把，只能有一把**）：
   `_apply_world_delta` 不接受 `run_scene` 开场读的那份副本，而在锁内自己重读。只给写加锁救不了（锁到了也只是把过时副本安全写进去）。
   锁是进程内的，同属契约 9。遗留边界：同分支并发的另一场是拿着开场旧世界演的，这是并发语义不是数据丢失。
7. **补写窗口内必须拒绝分叉**（`_pending_world_patch` + `fork_from_snapshot` 前置检查 `ConflictError` 409）：标记必须早于后置快照可见——
   引擎预生成快照 id、在 `create_snapshot` **之前**经 `on_after_snapshot` 回调挂上，**不要挪回 `run()` 返回之后**，也不要改用 `save_scene` 落库作可见性起点；
   摘除在评估+补写整个 try/finally 之后（成败均摘），且 `finally` 必须罩住 `engine.run()` 本身。机制细节见 `fork-snapshot.md`。
8. **世界变量进 system 是安全的前提是整场冻结**（契约 3 补充条款）：场景内会变的状态走 user 的【当前环境】，两者不得混用。

### schema 1 的其余约定

- 世界变量由导演评估的 `world_state_delta` 产出，只描述**世界层**持续性改变；`plan_scene` / `evaluate_scene` 都读 `world.variables`
  （看不见世界状态的导演会排出自相矛盾的场次）。
- 角色卡里"信息不对称"的 `unknown_facts` 管"不该知道的不知道"，`WorldState` 管"该传播的能传播"，边界是"这件事是不是公开的"。
- 陷阱 20 的 schema 1 部分：世界变量垫在 `scene_context` 之下，因此同样逐条进 system；见 `scene-engine.md` 陷阱 20 的适用范围表。
- 历史细节、旧叙述的取舍与评审出的五个洞：`docs/fix-tickets/07-world-state.md#trap19-reasoning`。

---

## 环境智能体现状（原 §13 设想表该行，已落地）

**【实况】** 环境智能体（`agents/environment_agent.py`）已落地：裁决角色对物件的尝试 → 环境回合；物件状态能跨场延续（schema 2）；
预制揭示与现场生成揭示分离；导演产出 `objects_present` 与 `environment_script`；前端有物件编辑器与世界面板。裁决走 OpenAI 原生调用（`chat_safe`，契约 7），**不用 AutoGen**。
**【设想】尚未实现**：环境主动事件（"叛军抵达城门"仍靠导演在场间写）、"定向通告"（只通知某些人）、按观看者过滤"目前对话"、
物件进 Kuzu 图谱（与工单 06）、指代消解（"*把它戴上*"）、角色自己写了结果时的自动纠正、私有内心 OS（工单 21）。

## 动手前的快速自检

- 我在往 system 里加东西吗？它整场不变吗？（契约 3）
- 角色 / selector 能看到的是**可见投影**，还是全量版本 / 隐藏规则？（契约 1）
- 这条规则对 schema 1、2 各自成立吗？有没有按 `world_schema_version` 分支？
- 新增写世界的路径：有 `request_id`（契约 5）、走 `commit_world_event`、先事务后记忆 / SSE 吗？
- 新增建场景路径：搬运 `objects_present` / `environment_script` 了吗？
