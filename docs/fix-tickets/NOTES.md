# 工单排期说明与结论记录

> 本文件收纳索引表容不下的**长说明**：拆分理由、排期依据、已合并 PR 的结论与教训。
> 索引本体见 [README.md](./README.md)，流程规则见 [CONVENTIONS.md](./CONVENTIONS.md)。
>
> 这里只记录**为什么这么排、这么拆、踩了什么坑**。代码事实以 `CLAUDE.md` 与代码为准。

---

<a id="bg"></a>

## 项目背景速览（给不熟悉本项目的 session）

- 项目：`PlotSystem`，多分支多智能体剧情推演系统。后端 FastAPI（`backend/`），
  前端 Vue3 + Vite + Pinia（`frontend/`）。
- **先读根目录 `CLAUDE.md`**：第 7 节九条契约是承重墙，
  第 12 节列出的已知缺陷与 dead code **不要顺手修**。
- 核心链路：实体抽取建图 → 角色卡生成 → **自研**场景引擎驱动多角色对话（非 AutoGen GroupChat）
  → 导演评估决策 → 快照/分支 → 总结输出。
- 数据存储：SQLite `data/projects.db`（`data_json` 列是唯一真相源）+ 角色卡 JSON 文件
  + Kuzu 嵌入式图库（单文件）+ ChromaDB 向量库。
- 启动：`npm run dev`（前后端同起）；单独：`npm run backend` / `npm run frontend`。
- 测试：`python -m pytest -q`（全量跑可能因 chromadb→onnxruntime 触发 DLL 崩溃，
  可单跑不涉及 chroma 的文件）。

---

<a id="t11"></a>

## 工单 11 — 从「Selector + 环境交互」拆成两单

原 11 同时包含「Selector 发言模式」与「动作/环境交互裁决」，二者体量与风险完全不同。
现拆为：**11 = Selector 打通（纯修复，已完成）**，**20 = 环境智能体（大提案，见阶段 D）**。

**实现方案是独立评分**（`backend/scene_engine/speaker_selector.py`）：
对每个候选角色各发一次轻量打分调用（`asyncio.gather` 并行，整轮延迟仍是一个 RTT），
一次只让打分器看一个角色，从而消除旧实现"把所有名字排成一列问 LLM"带来的位置偏见，
以及 `agent.name in choice` 子串包含的误匹配。

LLM 输出的三维分（发言欲望 / 相关度 / 主动性）再叠加两个纯本地信号：
被直呼其名加分（最长名优先匹配）、几何衰减的重复发言惩罚（压低但不硬禁连续发言）。
打分器有独立接入点（`LLM_MODEL_SELECTOR` / `LLM_SELECTOR_BASE_URL` / `LLM_SELECTOR_API_KEY`），
可挂本地小模型。

兜底全部可观测：单个候选失败取其余人的中位数、全部失败退化为纯本地打分并 warning，
不再静默回退 `agents[0]`。链路已打通：`Scene.speaker_mode` → 反序列化 → API/导演规划
→ `run_scene` 构造 `SceneConfig` → `apply_decision` 的 rollback 重演场景，
缺省值取 `settings.DEFAULT_SPEAKER_MODE`。非法取值在 `CreateSceneRequest`（422）
与 `Settings.DEFAULT_SPEAKER_MODE`（启动即失败）两处拦截，引擎兜底回退时 warning 一次。
降级还会写入 `DialogueTurn.selector_notice`，前端在角色名后以灰字展示（如"服务不可用：降级选择"）。

**未做（有意留下）**：让角色先生成一句草稿再竞价 —— 需要 N 次角色模型调用/轮，
成本约等于场景本身翻 N 倍，收益不确定，待 selector 跑出实测数据后再评估。

---

<a id="t16"></a>

## 工单 16 — Kuzu 快照持久化（已修复，留档）

这曾是排期里最容易被漏掉的坑，直接决定工单 06 能不能做：

- `GraphManager.checkpoint_to()` 用 `shutil.copytree()` 复制 `kuzu_db`，
  但当前 Kuzu 版本下 `kuzu_db` 是**单个文件**（实测 19–21 MB），不是目录 → 必抛 `NotADirectoryError`；
- 该异常被 `SnapshotManager.create_snapshot` 的 `except Exception → logger.debug` **静默吞掉**；
- 结果：`Snapshot.graph_checkpoint` 恒为空字符串，`restore_snapshot` 因找不到 checkpoint 而跳过，
  **快照从不包含知识图谱，回滚也不恢复图谱**。

**为什么当时不是 P0**：图谱只在 GraphRAG 构建阶段写入一次，之后全程只读。
既然内容不变，"回滚不恢复图谱"与"恢复了图谱"在效果上等价，因此当时无实际影响。

**修复内容（PR #15）**：`checkpoint_to` / `restore_from` 已按 `Path.is_dir()` 分支选择
`copytree` / `copy2`（新增模块级 `_remove_path` 处理删除），`create_snapshot` /
`restore_snapshot` 两处吞异常的 `logger.debug` 已提升为 `logger.warning` ——
**这个 bug 正是被日志级别藏起来的**。工单 06（动态图谱写回）的硬前置已解除。

---

<a id="pr15"></a>

## PR #15 — 为什么 23 / 03 / 19 / 16 合在一个 PR

四者是同一个用户可见故障的不同切面 —— "刷新一下推演数据就没了、快照功能几乎没用"。
拆开后任一个单独合入都不能让该场景可用：

- **23** 让后端真的有东西可恢复（逐轮落盘 + 状态对账）；
- **03** 让前端能找到并重连那些场景；
- **19** 让刷新后的决策状态不会退化成重复提交；
- **16** 则是快照面板上线后立刻暴露的真实缺陷。

审核时建议按"后端可恢复性 → API 新增/语义变化 → 前端重连与快照面板"三段看。

**留下的尾巴**：工单 03 的可选目标 6（创建场景时把 `scene_id` 写回 `Branch.scenes`）未做，
该字段仍恒为空数组；前端改用 `GET /projects/{id}/scenes?branch_id=` 查，不依赖它。

另外 PR #15 的 review 把 `fork_branch()` 里的 `restore_snapshot()` 拿掉了 —— 分叉本是
"开一条新 IF 线"，不该当场把主线的图谱与 Chroma 长期记忆就地回滚掉（这两者按项目共享、
不随分支隔离）。**因此分叉当时只登记来源快照**，新分支首场如何承接该快照、
`fork_conditions` 如何生效，归工单 08（已随 PR #16 落地）。

工单 08 已按此重写：先给分叉下严格定义（五条不变量 I1–I5），再分两阶段落地 ——
阶段 A 给长期记忆补分支维度（隔离性 I3 的架构前提），阶段 B 把 `fork` 与 `rollback`
收敛成同一个原语。**只改 `fork_branch()` 修不了这个问题**，原因见该工单 §1.2。

---

<a id="t17"></a>

## 工单 17 — 为什么把它提到 04 / 05 之前

用户面板、导演评估、最终总结智能体这三方要看的其实是同一份东西
（角色的情绪、记忆、位置、内心）。若各做各的，会出现三套口径不一致的读取路径。
17 建一层地基，04 / 05 及后续「总结智能体的角色内部视角」都复用它。

**已落地内容**：新增 `backend/services/inspection.py`
（`resolve_scene_states` / `load_character_state` / `inspect_character`）与
`CharacterInspection` 模型；新增 `GET /projects/{id}/characters/{cid}/inspect`；
`GET .../memory` 改为读快照（此前每次新建 `MemoryManager` → **恒返回空**）；
`DirectorAgent.query_character_state()` 从空壳落地到该层；
`orchestrator._load_inherited_states` 改为委托 `resolve_scene_states`，
契约 4 的四级快照继承从此只有一份实现。前端新增只读的 `CharacterInspector.vue`
（工作台与导演页均可打开）。

---

<a id="director-goal"></a>

## 导演的三层目标模型（27 → 04 → 28 → 18）

2026-09-03 复核得出的结论是：导演目前既"瞎评"又"健忘" —— 评估时拿不到角色卡，
规划时只看得到最近 5 条场景标题，而 `narrative_goal` 是一个**只活一次 HTTP 请求的参数**，
在 `apply_decision` 里直接被 `f"延续上一场（{name}）的剧情走向"` 替换掉，
连跑几场后系统只剩动量、没有引力。四张工单分工：

- **27** 抽出公用的上下文压缩（导演侧目前零预算，总结侧按字符截尾会丢结局）
- **04** 修掉评估空转与一批 P0 bug
- **28** 立只读的故事目标锚点与可观测度量
- **18** 给导演路线图与长期记忆

**顺序不可调**：没有 28 的锚点，18 的路线图就没有对齐对象。

### 三层的具体分工与两条设计禁令

三层 = **项目级只读锚点**（`narrative_goal`）/ **分支级路线图**（`storyboard.outline`，
导演可写留痕）/ **场次意图**。做 28 / 18 前必读以下两条（讨论已定，不要重新评估）：

1. **不允许导演修改 `narrative_goal`**。它既是评分基准又可被被评方改写的话，
   自评系统会把目标改成自己刚演出来的东西，度量归零。导演的可写空间只在第二层。
2. **不新增 `DecisionType.END`**。它会波及决策 CAS、幂等表与前端三处；
   结局用 `is_ending_reached` 标志表达即可。

### 28 落地时定下的口径（18 会直接继承，别再重新讨论）

工单 DoD 里没写、实现前与用户逐条确认过的选择：

| 议题 | 结论 | 理由 |
|---|---|---|
| 进度历史存哪 | **不新增存储**，评估时现算 | 分支级/项目级字段都要额外维护一份状态；现算天然跟随谱系 |
| 谱系怎么取 | `Scene.parent_scene_id` 链 + 同分支历史的并集 | 该链按不变量 I4 跨分支接上，IF 线不必从 0 重爬；手建场景链会断，故并入同分支 |
| 停滞怎么记 | `story_progress_raw` + `progress_stalled` 布尔，**不存累计次数** | 累计值又是一份跨场状态；要聚合让消费方按分支自己算 |
| `story_progress` 不可用 | 用 `-1`，且**不进** `is_evaluation_unavailable()` | 那个函数意为"整份评估作废"，漏一个键不该连带把决策打成保守默认 |
| plan 接口的用户输入 | 语义收敛为**本场意图** `scene_intent`，主线目标固定读 project | 保留成"临时覆盖主线目标"会让锚点形同虚设 |
| 目标可否事后改 | 新增 `PATCH /projects/{id}` | 建项目时种子还没传、角色还没抽出来，目标往往写不准；这是用户侧写入口，不违反禁令1 |
| 双空时的结局判定 | `narrative_goal` 与 `ending_criteria` 都为空则**强制 False** | 没有锚点时导演唯一能对照的就是自己刚演的内容 |
| 结局达成后的前端 | 保留三个决策按钮，另加提示与输出入口 | 结局是导演的判断，用户可以不认同；别让一个布尔值锁死操作 |
| 线索谁合并 | 导演输出更新后的完整列表，后端只去重截断 20 | 只有导演知道哪条本场被收束了 |

**验收 3（跑题对白使 `plot_deviation_score` 升高）需要真 LLM，不适合进单测**：
自动化只断言"主线目标确实进了 prompt"，偏离度的真实敏感度留人工手测。

---

<a id="pr17"></a>

## PR #17 — 27-A / 04 的落地内容与 review 教训

落地内容：`backend/utils/context.py`（`fit_lines` / `compact_lines`，四策略 + 最终预算校验）；
导演评估补入场景预设与角色卡（含 `unknown_facts`）；`opening_narration` 在 `next_scene`
自动路径上不再丢失；评估解析失败不再伪装成一份全 5 分的正常评估；导演三档温度。

**27-B（角色 / selector 迁移）刻意未做** —— 它动契约 3，验收必须是逐字符等价回归。

PR review 又抓出三条（已随同一 PR 修复，值得记住）：

1. **"不可用"不能用普通数值表达**。`-1` 分会撞进 `make_decision` 的 `< 4` / `< 5` 阈值规则
   被判成 rollback，而 rollback 会真的建分支、真的改剧情状态；同族缺陷是 `apply_decision`
   用裸的 `SceneEvaluation()`（四项 0.0）表示"没有评估"。已收敛到
   `unavailable_evaluation()` + `is_evaluation_unavailable()` 前置守卫。
   **新增哨兵值时必须同时找出所有阈值比较点。**
2. **预算是硬约束，不是"结果非空"**。行粒度丢弃解决不了"被强制保留的行自己就超预算"，
   必须在返回前做最终校验；单行截断要二分求精确解（`estimate_tokens` 对 CJK 权重不同）。
3. **压缩手段自身也要有预算**。`llm_summary` 原本把不限长的中段塞进一次摘要请求，
   越该压缩的场景越容易把摘要请求自己撑爆。

---

<a id="t08"></a>

## 工单 08 — 两轮 review 的教训与实现期踩坑

分叉语义收敛（五条不变量 I1–I5）的实现事实已写进 `CLAUDE.md` §6.3.1 与 §4.2 陷阱 11–12，
此处只留过程教训。

**最大的一条：一轮修复引入的新不变量，必须自己再走一遍边界。**
二轮 review 抳掉的两条规则都是**上一轮自己写下的**、看上去自洽但在分叉边界上错的：

- 「集合为空 = 未迁移」→ 分叉点本来就没记忆的新分支会把共享集合里“分叉之后”的记忆
  当成历史灌进来。已改为 `LEGACY_ADOPTED_KEY` 标记。
- 「分支 = 状态来源快照的分支」→ 新分支首场的 `restore_snapshot_id` 指向**来源分支**，
  只传 `scene_id` 的面板会查回主线记忆。已改为优先用场景自身分支。

三个实现期踩坑（再碰同类验证时直接拿走）：

1. `repository._deserialize_scene` **不还原 `created_at`** —— 两次 `get_scene` 得到的 dataclass
   必然不相等，验证“来源场景未被改动”要直接比 `data_json` 原文。
2. 测长期记忆隔离不要连真 embedding：monkeypatch
   `backend.memory.long_term.RemoteEmbeddingFunction` 成确定性假向量
   （需实现 `__call__` / `name` / `get_config` / `build_from_config` / `__init__`），Chroma 本体照常跑；
   并断言 `long_term._collection is not None`，否则降级路径会假通过。
3. 改 `build_character_agents` 签名要同步 4 处 `fake_build_agents` 测试桩。

---

<a id="t18"></a>

## 工单 18 — 分镜稿（storyboard）

导演目前只有提示词 + 压缩后的既往剧情，长线维持能力弱。
给导演一份可读写的持久化文件（类似 AI 的记忆文件），随快照一起版本化；
分支/回滚时需向导演说明与原线的差异。这是"导演 Heavy Duty 却配套工具不足"的正面解法。

**等 04 / 28 落地后再写工单文件**，接口会受其实现细节影响。

---

<a id="t26"></a>

## 工单 26 — 记忆写入点与固化水位线统一

PR #15 已修掉同族的"水位线落盘时机"（`run()` 新增 `on_persist`），
本单收尾剩下的两项：场景内自动固化绕过水位线、以及给长期记忆写入加确定性 ID 兜底。

后者的价值随环境层展开而放大：世界变量写回、裁决落档、动态图谱写回都会新增写入点，
靠"每个人都记得维护水位线"是维持不住的。

**必须排在工单 09 之前**：26 未做时长期记忆里可能混着重复条目，会直接干扰 09 的检索质量评估。

---

<a id="t09"></a>

## 工单 09 — 记忆检索质量

前置强调：15 已修复（写入范围改为在场感知 + 去重），
现在可以在干净数据上评估是否需要更强的重要度判定 / 分层加权。

---

<a id="contract3"></a>

## 待裁定：契约 3 在世界状态上的补充条款

工单 07 §3.3（开场冻结进 system）与工单 11 §2.2（“更新后的环境状态在下一轮
`build_system_prompt` 中体现”）**互相矛盾**，后者直接破 `CLAUDE.md` 契约 3
（system 只放整场不变的内容，prefix cache 靠它）。

**建议的裁定**（做工单 07 / 20 前需人类确认，确认后写进 `CLAUDE.md` 契约 3）：
世界状态分**场景常量**（开场即冻结，进 system）与**场景内变量**
（进 user 消息的“当前环境”块），**两者不得混用**。

---

<a id="t20"></a>

## 工单 20 — 环境智能体（为什么排最后）

承接介于「角色动作」与「环境变量」之间的判定。
例：配角想拔石中剑 → 判定"没拔动"；角色好奇触碰祭祀水盆 → 展示水盆的特殊功能。

实现走 **OpenAI 原生 function calling**，**不需要引入 AutoGen**
（AutoGen 的 GroupChat 编排会打破 `CLAUDE.md` 契约 3 的 system 静态 / user 动态结构与 prefix cache）。

它会改动 `SceneEngine` 的对话循环本身，风险最高，因此排最后。

---

<a id="t21"></a>

## 工单 21 — 私有内心 OS

与现在会落档的 `DialogueTurn.inner_thought` **是两回事** —— 前者是输出前的临时思考、不持久化。
与 20 是同一件事的两面（动作的裁决 vs 动作的酝酿），可考虑合并提案。

---

<a id="t22"></a>

## 工单 22 — MCTS / 多结局

当前是「每次只生成一场 + 采纳导演建议」= 已默认剪枝的贪心单路径，多结局靠人工从快照分叉。
只有当**场景评价与分镜稿都持久化**之后，树上才有可比较的节点价值，搜索才谈得上。
**不要提前上算法。**
