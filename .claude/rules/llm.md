---
paths:
  - "backend/utils/llm.py"
  - "backend/utils/usage.py"
  - "backend/utils/context.py"
  - "backend/memory/embeddings.py"
  - "backend/config.py"
  - "backend/agents/**"
  - "backend/scene_engine/continuation.py"
  - "backend/scene_engine/speaker_selector.py"
  - "backend/scene_engine/action_intents.py"
  - "backend/scene_engine/engine.py"
  - "backend/services/orchestrator.py"
  - "backend/graphrag_pipeline/**"
  - "scripts/replay_character.py"
  - "scripts/usage_report.py"
  - "tests/test_llm_usage.py"
---

# LLM 接入规则（出口 / 多路模型 / 调用计数 / 续写截断）

管：所有 LLM 调用必须遵守的出口约束、四路模型配置、调用计数、角色回复的续写截断。
**配置项的唯一真值是 `.env.example`**，这里不复制。

目录：四路异构模型 · 契约 7 细则 · 陷阱 26 · 陷阱 28

---

## 四路异构模型（原 §2.1）

`settings.director_model / character_model / summary_model / selector_model` 分别读
`LLM_MODEL_DIRECTOR / CHARACTER / SUMMARY / SELECTOR`，留空回退 `LLM_MODEL_NAME`。
温度约定：角色 0.8（创意）、导演 0.3（一致）、总结 0.7（平衡）、选择器 0.2（判断）；
导演的规划 / 评估 / 决策另有各自的 `DIRECTOR_*_TEMPERATURE`，以 `config.py` 为准。

selector 另有独立的 `LLM_SELECTOR_BASE_URL / LLM_SELECTOR_API_KEY`（留空复用主配置），
把"发言者打分"这类短输入短输出的判断挂到本地小模型。透传路径仍是 `chat()/chat_safe()` 的
`base_url` / `api_key` 参数，**契约 7 未破**。
`EMBEDDING_*` 留空复用 LLM 那套；**换 embedding 模型必须清空 `chroma_db/`**（维度不一致）。

## 契约 7 细则 — LLM 唯一出口

- 所有 LLM 调用必须经 `backend/utils/llm.py` 的 `chat()` / `chat_safe()`（tenacity 3 次指数退避、180s 超时、失败转 `LLMError`）。
  **禁止**在别处实例化 OpenAI 客户端；**唯一例外**是 `memory/embeddings.py`（Chroma 要求同步接口）。
- 模型名一律走 `settings`，禁止硬编码。
- 调用点必须传 `purpose=LLMPurpose.xxx`（见陷阱 26）。
- **空正文判失败**：服务商返回空正文时这里抛失败、日志带 `finish_reason`，不得 `content or ""` 当成功交回。
- **触顶不重试**：`finish_reason=length`（推理吃光 `max_tokens`）的空正文立即失败——同一 prompt 再发结果一样，
  只是白等退避、多花 token；其余空正文照常退避重试。两种都把该次尝试的 token 计入用量（陷阱 26），但不计成功调用。
- 与角色回复层分工：`llm.py` 管"根本没给正文"，`SceneEngine._character_reply` 管"给了正文但全是续写"（陷阱 28）。

## 陷阱 26 — LLM 调用计数（工单25）

只增观测，但前两条动了就是数据损坏：

- **计数点只在 `utils/llm.py`**（外加 `memory/embeddings.py` 这个例外）。调用方只传 `purpose`，漏标归 `untagged`；
  `tests/test_llm_usage.py` 用 AST 扫描钉住"每个 `chat` / `chat_safe` 调用点都标了用途"。
  计数本身出错只记日志，不得让调用失败；token 记**计费量**。
- **归属靠 ContextVar**（`utils/usage.py`），不靠参数传递：`create_task` / `gather` / `asyncio.to_thread` 都会复制上下文，
  所以 selector 并发打分、线程里的 embedding 自然归到同一场。
- **推送 evaluation 事件之后不得再 `save_scene`**：用户收到评估就能决策，continue 会改写这一场，再整份覆盖 `data_json` 会抹掉决策的改动。
  所以评估的调用装在**内层计数器**里、记在 `SceneEvaluation.llm_usage`，场景的计数随逐轮落盘带走。
- **`run_scene` 必须自己装计数器、在外层 `finally` 撤下**：AutoPilot 的自动 continue 是在上一轮 `run_scene` 里起的任务、复制了它的上下文，
  不重装会两轮混计，不撤下则收尾阶段（AutoPilot 决策、下一场规划）的调用会记到已落库的那场上。
  continue 的多段在已有计数上累加（`UsageMeter.seed`）。导演历史副本里清掉评估的计数（理由同分镜稿 patch：副本复制进每个快照）。
- 报表：`python -m scripts.usage_report`（只读）。新增 purpose / 改计数口径要同步 `LLMPurpose` 与报表。
  推导与取舍见 `docs/fix-tickets/25-llm-usage.md#trap26-detail`。

## 陷阱 28 — 角色回复先截续写、再解析（工单30 PR-30a）

背景：角色拿到的"目前对话"是逐行剧本，模型说完自己这一轮常常不停——替别人写台词、写 `【环境】…`（伪造裁决结果）、复读到输出上限。
解析进 `dialogue` 就是公开对白，进全场 transcript 与长期记忆；adjudicate 档下还等于绕过裁决器伪造事实。
实现：`scene_engine/continuation.py::trim_continuation`，由 `SceneEngine._character_reply` 调用。

约束：

- **截断必须早于 `_split_reply`**：动作意图抽取、transcript、记忆都吃解析结果，晚一步续写里的 `*动作*` 就被当成尝试去裁决。
  截下的内容**不挽救**（不当提示、不改写、不交给裁决器）。
- **标记只认"已知"的**：伪发言人标签（环境 / 旁白 / 导演… 及其自造变体）、参演角色名标签、角色 prompt 自己的区块标题
  （`PROMPT_SECTION_TITLES`，用例扫描 `character_agent.py` 钉住同步），以及**其他**参演角色 / 伪发言人后跟冒号，
  且冒号标签须在行首或空白、句末标点之后。**名字后不跟冒号不算**（"诺安大人说得对"），放宽冒号要求会误截正常台词。
  **不要放宽成"任意 `【…】` 都截"**：奇幻 / 游戏种子常用 `【火球术】` 标技能物品，放宽会吞正常台词、整段截光，场景抛错转 paused 且每次续跑卡在同一处。
- **本人名字标签只剥不截**（模型常写成 `*动作*` 换行 `本人: 台词`，截它会截掉台词）；**例外是本人标签自己复读**
  （某段与前文某段相同，或本人标签超过 3 处，就从那里截）。
- **不落空轮次**：截完什么都不剩就再要一次（`_REPLY_ATTEMPTS`=2），仍不行抛 `LLMError`（场景转 paused）。
- **`CHARACTER_MAX_TOKENS` 要给推理留余量**（默认 8000）：推理模型把推理 token 算进 `max_tokens`，设低了正常回复被吃光只剩空正文。
  上限只在失控时才真正花钱。
- **`DialogueTurn.output_notice`** 记下发生过截断（`已截断：…` / `已重新生成：…`，冒号前是前端标签）：只给导演 / 用户看，
  不进 prompt、记忆与任何逻辑；**只存字数与开头 40 字，不存截掉的原文**——伪造内容整段落盘，迟早被某个"渲染全部字段"的新代码带回角色视野。
- 这是**兜底不是根治**：没有标记的续写（直接用叙述口吻写出结果）挡不住。主因在服务商（4 位量化的服务商易续写 / 推理失控，8 位及以上几乎不），
  用 OpenRouter 时按量化过滤、preset 须在模型名里引用才生效；prompt 层修法（工单30c）暂缓。
  换模型 / 服务商 / 改角色 prompt 前先跑 `python -m scripts.replay_character`（带 `--run` 才调 LLM），它用同一个 `trim_continuation` 判污染。
  该脚本的基线走真实的 `CharacterAgent.build_messages` / `prompt_tail`，变体 A 靠锚点定位：**改角色 prompt 后锚点找不到会直接报错，跟着改脚本**。
- 实验数据、回放评测方法、服务商对比见 `docs/fix-tickets/30-character-output-boundary.md#trap28-detail`。
