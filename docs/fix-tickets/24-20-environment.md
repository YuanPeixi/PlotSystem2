# 工单24 / 20：动作化与环境层（设计单）

**优先级**：P3（但这是《玻璃王冠》实测暴露出的主线问题） ｜ **依赖**：29 ✅、07 ✅、11 ✅；25（软前置）
｜ **契约影响**：契约 1 / 3 / 5 / 6 / 7 / 8，陷阱 9 / 13 / 14 / 19 / 20

> **状态：设计已定稿（2026-10-03），按 §10 分 PR 落地。**
> 24（物件表 + 动作意图）与 20（环境校验与反馈）写成一份设计单：24 单独上线没有消费方，
> 两者的数据结构必须一起想清楚。背景讨论见 [NOTES.md#t24-20](./NOTES.md#t24-20)。
>
> 本单写目标、判据、已定设计与红线，不写分步实现。§9 的决策已由 owner 授权按建议拍板；
> 实现中若发现与判据冲突，先改本单再改代码。

---

## 1. 现象

《玻璃王冠》实测，场景进行中王冠**从未有过反应**：

- 角色反复对王冠做动作（"将王冠举过头顶"），下一轮无人接，甚至出现空回合；
- 角色在动作里**自己替世界宣布结果**（"暗格开启的细微声响…"），因为没有裁决者；
- 两次投影都只出现在导演为**下一场**写的开场白里，且都违反种子规则
  （碰一下就触发、伊莎贝尔戴上却投出王后的记忆）；
- 物件在系统里没有落脚点：图谱只有 Character / Location / Event / Concept，运行时也不读图谱。

## 2. 为什么必须做，以及为什么不照搬 MiroFish

不做的话，"导演宏观、角色微观"（CLAUDE.md §1.4）在物件这一层是断的：角色只能自说自话，
导演只能在场与场之间凭印象补，规则一致性无从保证。

**MiroFish 的做法（物件当角色发言）不行**：物件会有"意图"、会在轮次里抢发言。
正确形态是**物件是被动规则，触发源是角色动作**。

## 3. 目标形态与范围

```
角色 *戴上王冠*（只写尝试，不写结果）
  → 环境校验：能不能做？会触发什么？（按物件的隐藏规则裁决）
  → 环境回合（公开，进"目前对话"，与【旁白】同位，不破契约3）
  → 揭示内容三级来源：
     ① 导演预制：规划这一场时写好，只给导演/环境层看
     ② 现场生成：只能从**当事人自己的** known_facts 与记忆取材
     ③ 信息不足：只给可观察现象，内容留给导演在评估/下一场补
```

**第一版做**：物件表；动作意图抽取；环境裁决与环境回合；三级揭示来源；场景内物件状态；
场景结束后把物件公开状态沉淀进 `WorldState`。

**第一版不做**：
- 环境**主动**事件（"叛军抵达城门"）——仍靠导演在场间写；
- 按观看者过滤"目前对话"（transcript 仍是全员共享的一份，见 §7 已排除 6）；
- 物件进 Kuzu 图谱（与 06 一起做，免得 schema 改两遍）；
- 指代消解（"*把它戴上*"不命中预过滤，见 §5.1）；
- 角色自己写了结果时的自动纠正（靠格式规范压制，裁决以隐藏规则为准）；
- 私有内心 OS（21）。

## 4. 数据模型（判据级，字段名可调）

| 模型 | 要点 | 存放 | 可见性 |
|---|---|---|---|
| `WorldObject` | `object_id`、`name`、`aliases`、`public_description`、`hidden_rules`、`visibility`（`global` / `character:{id}` / `hidden`，**与 29 同一套 scope**） | 文件 `objects/{object_id}.json`，项目级，同角色卡 | 公开描述按 scope 进角色 system；**隐藏规则只进导演与环境层** |
| `ActionIntent` | `index`、`text`、`object_id`、`verb`、`detail`、`status` | 挂在 `DialogueTurn.actions` | 导演 / 用户 |
| `DialogueTurn` 扩展 | `kind`（character / environment）、`narration`、`private_detail`、`perceived_by`、`source_turn_id`、`source_action_index`、`revision` | 随 `dialogue_log` 存 `scenes.data_json` | `private_detail` 只给导演 / 用户 / `perceived_by` 的记忆 |
| `Scene` / `SceneConfig` 扩展 | `objects_present`、`environment_script`（导演预制揭示）、`environment_state`（场景内物件公开状态） | `scenes.data_json` | `environment_script` **绝不进 `initial_conditions` / `scene_context`**（陷阱 20） |

`ActionIntent.status`：

| status | 含义 | 续跑时 |
|---|---|---|
| `recorded` | 已抽取、不裁决（`ENVIRONMENT_MODE=record`） | 不补裁决（开关打开后不追溯生成环境回合） |
| `pending` | 已抽取、待裁决 | **补裁决** |
| `resolved` | 已产生环境回合（含"未触发"的回合） | — |
| `skipped` | 预过滤未命中 / 抽取判定不构成尝试 / 抽取失败 / 环境回合超上限 | — |
| `failed` | 裁决调用失败 | 不重试（否则崩溃—续跑会无限循环） |

**环境回合也是 `DialogueTurn`**：逐轮落盘、水位线（陷阱 9）、续跑重放、在场记忆都自动适用，
不另开存储。走 5.4 checklist：`_deserialize_scene` 必须同步新字段；前端 `types/index.ts` 同步。
`kind` 在 PR-0 先行引入（恒为 character），让计数口径的改动可以先于环境回合落地。

## 5. 运行时：判据与已定设计

### 5.1 主循环

```
角色轮次产生
 ├─ 解析：每个 *动作* 段各生成一个 ActionIntent（`turn.action` 拼接字段保持不变）
 ├─ 预过滤（本地子串匹配，零 LLM）：段文本是否含本场 objects_present 的 name / alias
 │    未命中 → skipped（绝大多数 *神态* 走这条）
 ├─ 意图抽取（selector 小模型，**一轮最多一次调用**，批量处理本轮命中段）
 │    输入：命中段 + 本轮对白 + 候选物件的 name 与**公开描述**（不给隐藏规则，免得预判结果）
 │    输出：object_id ∈ 候选 / verb / is_attempt / detail
 │    不构成尝试、object_id 越界、调用失败 → skipped
 │    构成尝试 → record 档置 recorded；adjudicate 档置 pending
 ├─ 落盘 + 推 SSE `turn`（第一次落盘时意图已完整）
 ├─ [adjudicate] 环境裁决（EnvironmentAgent，导演模型，温度 0.3，JSON 文本输出）
 │    输入：物件公开描述 + 隐藏规则 + environment_state + 本物件的 environment_script
 │         + 动作原文与执行者 + [仅 20b] 当事人的可见视图（§7 R2）
 │    输出：executable / triggered / reveal_source / narration / private_detail / state_changes
 ├─ 校验：state_changes 只许动本物件的键、不得撞保留字、塌单行、过预算；环境回合未超上限
 ├─ 一次 save_scene 原子提交：追加环境回合 + 动作置 resolved + 来源轮次 revision+1
 │    + environment_state 更新（同在 scenes.data_json，天然原子，无需跨库事务）
 ├─ 写记忆（§5.3）
 └─ 推 SSE：`turn_update`（来源角色轮次）→ `turn`（环境回合）
```

**抽取放在第一次落盘之前**（2026-10-03 细化）：抽取期间崩溃，这一轮还没落盘，等于没发生，
续跑重新生成；落盘的轮次意图恒完整，续跑只需补"裁决"一种半截状态，不需要"待抽取"状态与补抽逻辑。
代价是命中物件的那一轮多等一次小模型调用；未命中的轮次零延迟。

**裁决走 JSON 文本**（同 `evaluate_scene` 的 `_extract_json`）：`utils/llm.py` 目前只有文本补全。
真要用 function calling，扩展 `llm.py` 本身，仍是唯一出口（契约 7）。

### 5.2 推送与前端一致性

**问题**：角色轮次在 pending 时就推了 `turn`，裁决后它的 `actions` 状态变了；而 CLAUDE.md §8
要求客户端按 `turn_id` 去重，同一 `turn_id` 再推一次 `turn` 会被前端直接丢掉。

**已排除：延后推送角色轮次直到裁决完成。** 两个洞都堵不上：
1. 落盘（pending）与推送之间，重连客户端的铺底 GET 会拿到 pending 版本，随后到达的终版 `turn`
   被去重吞掉；
2. 崩溃续跑时 pending 版本是上一个进程落的盘，客户端 GET 到的必然是它，终版只能以"更新"的形式到达。

**已定设计**：
- 新增 SSE 事件 **`turn_update`**，载荷是完整的 `DialogueTurn`；`turn` 的"追加 + 按 `turn_id`
  去重"语义**不变**；
- `DialogueTurn.revision`：服务端每次改写一条**已落盘**的轮次就 +1；
- 前端 `turn_update`：本地有同 `turn_id` 且 `revision` 更大才替换，本地没有则忽略
  （终态 `reconcileLog` 无条件以持久化日志为准，会补齐）。
  **必须比 revision**：订阅期间铺底 GET 可能先拿到新版，排在队列里的旧事件随后才到；
- CLAUDE.md §8 的 SSE 事件列表与 §9.2 的"SSE 双保险"同步登记。

### 5.3 记忆分发

**问题**：现在"这一轮我能感知到什么"在**两处**各判一次：
- 现场路径：`SceneEngine._remember` 算 `from_self`，传给 `MemoryManager.add_experience`，
  后者再传给 `_turn_to_text` 与 `EpisodicMemory.record`；
- 重放路径：`EpisodicMemory.replay` 内部用 `turn.character_id == self_character_id` 再判一次。

环境回合引入第三个维度：`private_detail` 的可见性取决于 `perceived_by`，**与"是不是我说的"无关**
（环境回合没有说话人）。一个 `from_self` 布尔装不下，两处各自扩展迟早漂移——
而漂移的后果是 episodic 的现场条目与重放条目对不上，去重失效（工单26 复盘第 9、10 条的同类）。

**判据**：
- 感知判定收敛成**一个纯函数** `perceive(turn, viewer_id) -> Perception`，现场与重放路径**都只调它**；
- `MemoryManager.add_experience(turn)` **去掉 `from_self` 参数**；`EpisodicMemory.record` /
  `replay` 去掉布尔与 `self_character_id` 参数——两者构造时已持有自己的 `character_id`；
  引擎的 `_remember` 只负责"对本场全体参演角色各调一次"，不再判定任何东西；
- 对任意轮次 × 任意观察者，**现场写入与重放产出的 episodic 条目逐字相同**——用参数化用例钉住；
- 非当事人的重要性判定不得读 `private_detail`（与"他人内心独白不参与判定"同理，契约 1）。

**改动面**：

| 位置 | 改什么 |
|---|---|
| `SceneEngine._remember` | 不再算 `from_self` |
| `MemoryManager.add_experience` | 去掉 `from_self`，内部调 `perceive` |
| `MemoryManager._turn_to_text` | 改调统一渲染（§5.7），按 `Perception` 拼内心独白 / `private_detail` |
| `MemoryManager.replay_episodic` | 签名不变，内部不再传 `self_character_id` |
| `EpisodicMemory.is_important` / `record` / `replay` / `_snippet` | 布尔参数换成 `Perception` |
| 短期缓冲 / 长期记忆元数据 | 环境回合的 `speaker` 记为固定值（"环境"），`is_self=False` |
| 测试 | 所有传 `from_self=` / `self_character_id=` 的用例 |

### 5.4 续跑

- `run()` 在 `_replay_unconsolidated` 之后、主循环之前，扫 `dialogue_log` 里所有 `pending` 动作，
  **按日志顺序补裁决**，再进入发言循环；
- 环境回合的 `turn_id` 由 `(source_turn_id, source_action_index)` 确定性生成（uuid5，保持 hex 形态），
  补裁决前先查日志里是否已有同 ID 的回合；
- 补出来的回合照常推 `turn_update` + `turn`。

### 5.5 计数口径（同一口径，几处一起改）

- `turn_number` 仍是全局序号（含环境回合）；`turns_completed` 保持 `len(dialogue_log)`，
  与水位线 `turns_consolidated` 同一口径，不动；
- **`max_turns` 只数角色轮次**；另设每场环境回合上限 `MAX_ENVIRONMENT_TURNS`（默认 8），
  超限的动作置 `skipped` 并 warning；
- **轮询选人**按角色轮次数取模——否则每插一个环境回合轮转就错一位；
- **停滞检测**只看角色轮次——否则两次连续的环境回合会被判成"对话停滞"；
- ⚠️ **`apply_decision` 的 continue**：`max_turns = turns_completed + extra` 改为角色轮次数 + extra。
  这一处在编排层，最容易漏；
- selector 的"被点名"与"重复发言惩罚"跳过环境回合；
- 前端 `StageView` 的"第 N / max_turns 轮"改数角色轮次。

PR-0 先把前四项后端口径改成"数角色轮次"（此时恒等于全部轮次，行为不变），PR-2 再引入环境回合。

### 5.6 场景内状态与世界变量（契约 3 补充条款）

| 层 | 何时变 | 放哪 |
|---|---|---|
| `Scene.environment_state` | 场景进行中，每次裁决 | 角色 **user** 消息里独立的【当前环境】块 |
| `WorldState` | 场与场之间 | system 的"当前情境"（工单07 已落地） |

- 场景结束时 `environment_state` 转成 world delta，与评估的 `world_state_delta`
  **在同一把分支锁、同一个 `_pending_world_patch` 窗口内**合并：先环境 delta，后评估 delta
  （导演可修正）；
- **评估失败时环境 delta 照样要落**——它是已经发生的事实。现在评估失败会整段跳过世界变量更新，
  这里要拆成两个独立的 `try`；
- 键名 `物件名·属性`（如 `王冠·佩戴者`）；占世界变量预算，PR-2 实测后再决定是否给物件单独分额度；
- `environment_state` 只许放**所有在场角色都能感知**的公开状态（契约 1）；私密结果只进
  `private_detail`。

### 5.7 失败语义、未触发与渲染统一

- 抽取失败 → `skipped` + warning（等价于"没识别出尝试"，不伪造结果）；
- 裁决失败 → `failed`，**不生成环境回合**，推非致命 `scene_error`。
  **绝不能写"（毫无反应）"**——那是凭空断言一个事实（与陷阱 14 同理）；
- **裁决成功但未触发规则，照样生成环境回合**（"王冠冰凉，毫无动静"）：这是裁决出的事实，
  也正是要解决的"动作悬空无人接"。与失败的区别在于有没有裁决结果；
- `ENVIRONMENT_MODE=off` 时行为与现在逐字一致；
- **前置重构：统一轮次渲染**（陷阱 13 的同类风险）。现状与处置：

  | 渲染点 | 处置 |
  |---|---|
  | 引擎 transcript 行、导演评估、总结、记忆文本、`scripts/run_demo` | **收成一个 `render_turn`**，PR-0 完成，输出逐字不变 |
  | `EpisodicMemory._snippet` | **格式冻结不并入**：它是序列化格式，老快照与重放去重靠逐字相同，改格式即去重失效 |
  | selector 点名检测、停滞签名 | 不是渲染而是信号文本；PR-2 按 `kind` 过滤掉环境回合，不改拼法 |

## 6. 物件存储与预算

`objects/{id}.json` 摆在项目目录里、**明确支持人工编辑**——与 `world_state/*.json` 同样会被
手写超长内容绕过写入侧闸门（陷阱 19 的教训）。而物件公开描述进的是**每个可见角色、每一轮**的
system prompt，别名决定预过滤的命中率（即成本）。

**三道闸门，读取侧不可省**：

| 闸门 | 位置 | 行为 |
|---|---|---|
| 写入 | 构建期抽取 | 超限截断 + warning |
| 写入 | 用户 PATCH | 超限直接 **422，不截断**（同分镜稿 PUT） |
| 读取 | `repository` 反序列化时调 `clamp_object` | 压回预算，**只压不写回**，warning |
| 引擎 | 构造时 | `objects_present` 条数 / 渲染总 token 再拦一次（构造参数谁都能传） |

**预算与规整规则**（数值在 PR-1 定，口径先定）：
- `name` / 每条 `alias` / 每条 `hidden_rule`：渲染时塌单行（"一行一条"约束的是**渲染结果**，
  不只是值，陷阱 19 第 5 条教训）；存储可保留换行；
- `aliases`：条数上限；**单字别名丢弃**、**与任一角色名相同的别名丢弃**，并 warning——
  否则预过滤每轮都命中，抽取成本失控；
- `public_description` / `hidden_rules`：单条与总量 token 上限；
- `visibility` 非法或缺失 → **按 hidden 处理**（失败即收紧，与 29 一致）；
- 文件名与 JSON 内 `object_id` 不一致 → 以文件名为准并 warning；
- 场景级：`objects_present` 条数上限、角色 system 里物件描述总 token 上限、
  `environment_script` 条数上限、`environment_state` 键数与总 token 上限。

纯函数放 `services/objects.py`（同 `world_state.py` / `storyboard.py`：拆出来是为了 import 方向）。

## 7. 红线与已排除

**红线**：

- **R1 隐藏规则与预制揭示只进导演与环境层**：不进 `scene_context`、角色 system / user、
  selector 打分 prompt、意图抽取 prompt（契约 1，陷阱 20）；
- **R2 现场生成（20b）结构上拿不到 `unknown_facts`**：裁决器的输入只接受
  `inspect_character(include_private=False)` 产出的视图，不靠提示词约束"别用"；
- **R3 "目前对话"只写 `narration`**：`private_detail` 不进 transcript；
- **R4 场景内变化的状态只进 user 块**，不回写 system（契约 3）；
- **R5 落盘先于推送**：环境回合与状态更新同一次 `save_scene`，之后才推 SSE（工单23）；
- **R6 新的记忆写入路径必须过水位线**：环境回合作为普通 turn 走 `_remember`，不另开写入口（陷阱 9）；
- **R7 "只写尝试、不写结果"的格式规范与 20 同时上线**：只上 24 会让动作悬空无人接；
- **R8 LLM 调用一律走 `chat_safe`**（契约 7）；裁决器由 orchestrator 构造后注入引擎，引擎不碰
  SQLite（契约 8）；
- **R9 `EpisodicMemory` 的条目格式不动**（§5.7）。

**已排除**：

1. **物件当角色发言**（MiroFish）——见 §2；
2. **环境状态每轮写回 system**（工单 11 §2.2 原方案）——破契约 3，已作废；
3. **延后推送角色轮次代替 `turn_update`**——见 §5.2；
4. **`from_self` 之外再加一个 `perceived` 布尔**——两条路径各判一次的结构不变，迟早漂移，见 §5.3；
5. **裁决失败时生成"无反应"回合兜底**——见 §5.7；
6. **按观看者过滤 transcript 支持私密环境回合**——要改 transcript 的结构与 prefix cache 的
   前缀共享方式，第一版用"公开 narration + 当事人 private_detail"代替；
7. **为物件另建分支级状态存储**——第一版复用 `WorldState`（NOTES#t24-20）；
8. **抽取放在落盘之后**——多一种半截状态，见 §5.1。

## 8. 验收

沿用 CONVENTIONS §7：每道防护撤掉后必须有用例变红。

**《玻璃王冠》三条主用例**：
- 伊莎贝尔戴上王冠 → 投出的是**她自己的**记忆（20b）；
- 塞芙拉碰一下 → 生成"未触发"的环境回合；
- 暗格必须照诺安给的方法才开。

**结构性用例**（节选）：
- 隐藏规则不出现在任何角色的 system / user、selector 打分、意图抽取 prompt 中；
- 20b 构造"非亲生"进伊莎贝尔 `unknown_facts`，断言它不出现在裁决 prompt 里；
- 在"动作已落盘、裁决未出"时中断，续跑后只补出**一次**环境回合；
- 两个环境回合相连不触发停滞；含环境回合的场景 continue 后角色轮次数正确；
- `perceive` 参数化用例：现场写入 = 重放产出（§5.3）；
- 前端：铺底拿到新版后旧 `turn_update` 到达不回退；本地没有该轮时 `turn_update` 被忽略；
- 手写超长 / 单字别名 / 非法 visibility 的物件文件：读取被压回、不写回、visibility 收紧为 hidden；
- `ENVIRONMENT_MODE=off` 时全量既有测试不变；纯神态动作零 LLM 调用（靠 25 的计数断言）。

## 9. 已定决策（2026-10-03，owner 授权按建议拍板）

| # | 问题 | 结论 |
|---|---|---|
| A1 | `objects_present` 由谁定 | 导演规划时选、用户在 SceneComposer 可改；不做按地点自动推断 |
| A2 | `private_detail` 是否存进 `dialogue_log` | 存。与 `inner_thought` 同级：导演 / 用户可见，transcript 与他人记忆不可见 |
| A3 | 环境回合上限 | `MAX_ENVIRONMENT_TURNS`，默认每场 8 |
| A4 | 开关形态 | `ENVIRONMENT_MODE = off / record / adjudicate`，默认 off；PR-1 合 main |
| A5 | 物件公开状态的世界变量键名 | `物件名·属性`；独立额度待 PR-2 实测 |
| A6 | 当事人记忆里 narration 与 private_detail 的形态 | 拼成一条（当事人 = 公开叙述 + 私密细节；他人 = 公开叙述） |
| A7 | 已有项目补抽物件 | 新脚本 `scripts/extract_objects`，沿用 29 的预览 / `--apply` / 失败整个不写 |
| A8 | 状态更新推送 | `turn_update` + `revision`（§5.2） |
| A9 | 抽取时机 | 第一次落盘之前（§5.1） |
| A10 | 未触发规则 | 照样生成环境回合（§5.7） |

## 10. PR 拆分

| PR | 内容 | 合入后的行为变化 |
|---|---|---|
| **PR-0** | ① `render_turn` 收拢五处 transcript 式渲染；② `perceive` 收敛感知判定，去掉 `from_self` / `self_character_id`；③ `DialogueTurn.kind`（恒为 character）+ 反序列化 + 前端类型；④ 计数辅助函数，轮询选人 / 轮次上限 / 停滞检测 / continue 改数角色轮次 | 无（golden 字符串用例钉住渲染输出逐字不变） |
| **25** | 场景级 token / 调用计数（独立成单，可与 PR-1 并行） | 只增观测 |
| **PR-1 = 24** | `WorldObject` 模型 / 存储 / 三道预算闸门；构建期抽取 + 可见性（复用 29 分类器）；物件 CRUD API（PATCH 带幂等键，契约 5）+ Workspace 编辑器；`DialogueTurn.actions` + 预过滤 + 意图抽取；`ENVIRONMENT_MODE` off / record；`scripts/extract_objects` | 默认 off 无变化；record 档可评估命中率与成本 |
| **PR-2 = 20a** | EnvironmentAgent（揭示来源 ① ③）；引擎集成（环境回合、续跑补裁决、计数口径的 `kind` 过滤）；`turn_update` + `revision`；【当前环境】块；格式规范"只写尝试"；`plan_scene` 产出 `objects_present` / `environment_script`；环境 delta 并入世界变量；前端显示环境回合 | `adjudicate` 档可用 |
| **PR-3 = 20b** | 揭示来源 ②（当事人可见视图）；《玻璃王冠》整场验收；成本报告 | — |

收尾：CLAUDE.md 第 13 节"环境智能体"移入正文（【设想】→【实况】），新增陷阱条目；
§8 SSE 事件、§9.2 前端要点、5.4 checklist 涉及的反序列化函数同步登记。

## 11. 线索

- 引擎主循环、解析、选人、记忆分发：`scene_engine/engine.py`；停滞判定：`scene_engine/termination.py`；
- transcript 式渲染五处：引擎 transcript 行、`director_agent` 评估用对白、`summary_agent`、
  `memory_manager._turn_to_text`、`scripts/run_demo`；
- 感知判定两处：`SceneEngine._remember`、`EpisodicMemory.replay`；
- 物件抽取与可见性：`graphrag_pipeline/world_rules.py`（`LoreVisibilityClassifier`、`lore_for_character`）；
- 预算与读取侧压回的参照：`services/world_state.py::clamp_world_variables`、
  `services/storyboard.py::clamp_storyboard`、`repository._deserialize_world_state`；
- 世界变量合并与补写窗口：`orchestrator._apply_world_delta`、`_branch_lock`、`_pending_world_patch`；
- continue 的轮次计算：`orchestrator.apply_decision`；
- 逐轮落盘与 SSE：`orchestrator.run_scene` 里的 `on_turn`；前端 `stores/scenes.ts` 的 `openStream` /
  `reconcileLog`；`StageView` 的轮次进度；
- **易漏同步项**：`_deserialize_scene`（新字段）、`types/index.ts`、CLAUDE.md §8 SSE 事件表、
  `CharacterAgent` 格式规范、`make_decision` 不受影响（不加 LLM 调用，陷阱 22）。
