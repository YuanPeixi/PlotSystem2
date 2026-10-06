# 工单25：场景级 LLM 调用与 token 计数

**优先级**：P2（工单20 的软前置） ｜ **依赖**：无 ｜ **契约影响**：契约 7（唯一出口不变）、契约 5 / 工单23（落盘时序）

> 本单写目标、判据与红线，不写分步实现。背景见设计单
> [24-20-environment.md](./24-20-environment.md) §10 与 §8 的"纯神态动作零 LLM 调用"验收项。

---

## 1. 现象

系统里没有任何地方记录"一场戏花了多少次 LLM 调用、多少 token"：

- 每轮至少一次角色调用；selector 模式下每个候选再各一次打分；记忆检索走远程 embedding；
  上下文超预算时还有压缩调用；场景结束有一次评估；
- 工单24 的 record 档又给命中物件的轮次加了一次意图抽取，工单20 的 adjudicate 档还要再加
  一次裁决（20b 的揭示来源 ② 再加一次生成）；
- 这些成本现在只能靠读代码估。`scripts/action_stats` 数得出抽取调用次数，但数不出 token，
  也数不出其他用途的调用作对照。

## 2. 为什么要先做

- 工单20 的环境回合额度（`MAX_ENVIRONMENT_TURNS`，默认 8）与预过滤的取舍，需要真实的
  "每场多花多少"作依据，不是拍脑袋；
- PR-3（20b）要出成本报告，没有计数就只能写估算；
- 设计单 §8 的验收"纯神态动作零 LLM 调用"需要一个能按用途断言调用次数的机制；
- 只增观测、不改行为，风险最小，适合先于 PR-2 落地。

## 3. 判据（DoD）

### 3.1 计数点只有一个

- 计数发生在 `backend/utils/llm.py` 内部（契约 7 的唯一出口）。调用方只标明**用途**，
  不自己计数；
- 用途至少区分：角色发言、selector 打分、动作意图抽取、上下文压缩、场景评估、
  导演规划、输出总结、构建期抽取，以及工单20 预留的裁决 / 生成。新增调用点必须标用途，
  漏标的归入"未标注"一类且可见，而不是静默丢失；
- 每个用途记：成功调用次数、失败次数（重试耗尽）、重试次数、输入 token、输出 token、
  累计耗时。服务商响应带 `usage` 时取实数；不带时按 `estimate_tokens` 估算，并**标明这是估算**；
- 远程 embedding（`memory/embeddings.py`，契约 7 唯一的例外客户端）同样计入，用途单列。
  它在 `asyncio.to_thread` 里调用：计数机制必须在线程里也能归到正确的场景，
  且计数器的累加要考虑线程并发。

### 3.2 归属到场景

- 一次 `run_scene` 期间发生的调用归属于这场戏，结果随场景落库（`Scene` 新字段，
  continue 的多段**累加**而不是覆盖）；
- 场景评估的调用归属于那份评估（`SceneEvaluation` 新字段），不记在场景上（理由见 §4 R1）；
- 不属于任何场景的调用（构建、规划、输出）本单不要求持久化，至少要在日志里有汇总；
- 同一时刻跑着的两场戏，计数不得串。

### 3.3 可读

- 新脚本 `scripts/usage_report --project ID [--branch B]`：**只读**、不调 LLM，按场景与用途
  列出调用次数与 token，并给出合计；
- 场景详情 API 自然带出新字段（`to_dict`），前端类型同步；本单**不做**前端展示；
- 测试里能直接拿到一场戏的计数，断言"某用途调用次数为 0"。

### 3.4 不改行为

- 所有 LLM 调用的参数、次数、顺序与现在完全一致；计数失败（例如服务商返回的 `usage`
  结构异常）不得让调用本身失败。

## 4. 红线与已排除

**红线**：

- **R1 推送 evaluation 事件之后不得再 `save_scene`**。用户收到评估就能提交决策，continue
  会改写这一场（状态改回 pending、`max_turns` 变大）；之后再整份覆盖写 `data_json`，
  会把决策的改动抹掉。这正是评估的计数记在 `SceneEvaluation` 而不是 `Scene` 上的原因；
- **R2 `run_scene` 必须自己装一个新的计数器，不能沿用继承来的**。AutoPilot 的自动 continue
  是在 `run_scene` 内部 `create_task` 起下一轮的，新任务会复制父任务的上下文；不重新装，
  两轮的数会混在一起；结束时要撤下，否则收尾阶段（AutoPilot 决策、下一场规划）的调用
  会被记到已经落库的那场上；
- **R3 计数器不进任何 prompt、不参与任何判断**（`make_decision` 不读它，陷阱 22）；
- **R4 落盘时序不变**：不为计数多加一次 `save_scene`，随既有的逐轮落盘与 `on_persist` 写入即可
  （工单23 的"落盘先于推送"不受影响）。

**已排除**：

1. **把计数器作为参数逐层传进每个调用点**：要改所有 agent 的签名，新增调用点很容易漏传，
   而漏传是静默的；
2. **全进程一个总计数器**：同时跑的两场戏分不开，AutoPilot 连跑时也分不清哪一场花了多少；
3. **把计数写进独立的 SQLite 表**：需要索引 / 过滤时再说，现在随 `data_json` 携带即可
   （CLAUDE.md §5.4 第 3 步）。

## 5. 验收

沿用 CONVENTIONS §7：每道防护撤掉后必须有用例变红。

- 一场 round_robin 的 N 轮戏：角色发言调用次数 = N（mock LLM），评估调用记在评估上、不在场景上；
- selector 模式：打分调用次数 = 候选数 × 选人次数，与角色发言分开计；
- 两场戏并发跑（各自 mock 不同的调用次数），计数互不串；
- continue 续跑后，场景上的计数是两段之和；
- AutoPilot 自动 continue：新一轮的计数从新计数器开始累加到场景上，不重复计入上一轮的；
- 服务商响应不带 `usage`：token 按估算记录且标明估算，调用本身成功；
- `usage` 结构异常（字段缺失、类型不对）：调用照常返回，计数记为估算；
- 重试一次后成功：成功次数 1、重试次数 1；三次全失败：失败次数 1；
- 在 `asyncio.to_thread` 里发生的 embedding 调用归到正确的场景；
- 推送 evaluation 事件之后到 `run_scene` 结束，没有 `save_scene` 发生（钉住 R1）；
- 旧数据（无计数字段）的场景与评估照常读取；字段内容损坏时降级为空计数并 warning，不得让
  `list_scenes` 报错；
- record 档下纯神态动作的轮次：意图抽取用途的调用次数为 0。

## 6. 线索

- 唯一出口：`backend/utils/llm.py`（`chat` 带 tenacity 重试，`chat_safe` 包一层转 `LLMError`）；
- embedding：`backend/memory/embeddings.py`，经 `memory/long_term.py` 的 `asyncio.to_thread` 调用；
- 调用点：`agents/character_agent.py`、`agents/director_agent.py`（规划 / 评估）、
  `agents/summary_agent.py`、`scene_engine/speaker_selector.py`、`scene_engine/action_intents.py`、
  `utils/context.py`、`graphrag_pipeline/`（entity_extractor / persona_builder / world_rules）；
- 归属边界：`orchestrator.run_scene`（含 `_autopilot_after_scene` 在运行锁释放后执行）；
- **易漏同步项**：`repository._deserialize_scene` 与评估的反序列化（CLAUDE.md §5.4）、
  `frontend/src/types/index.ts`、CLAUDE.md §4.1 模型表与 §10.3 脚本清单、`docs/fix-tickets/README.md`。
