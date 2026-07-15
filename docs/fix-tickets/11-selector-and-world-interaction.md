# 工单11：完善 Selector 发言模式 + 动作/环境交互处理

**优先级**：P2
**预估改动范围**：中-大（涉及场景引擎、模型层、可能涉及知识图谱回写）
**依赖**：强烈建议先完成工单07（WorldState，动作副作用需要一个落点）；
可与工单06（动态图谱回写）联动，但不强制依赖

---

## 1. 背景

2026-07-15 的架构讨论中确认了两个现状缺口，此前的工单列表（01-10）均未覆盖，属于遗漏：

1. **Selector 发言模式几乎是占位实现**。`backend/scene_engine/engine.py` 的
   `_llm_select_speaker()` 只是每轮一次性问 LLM"接下来谁该发言"，用
   `if agent.name in choice` 做字符串包含匹配来对应角色——没有候选者 ID 约束、
   没有结构化输出、名字重复/子串包含时会误判、失败时静默兜底为 `self.agents[0]`
   （即"没选出人时永远选第一个角色"，长场景下会造成隐藏的发言偏向）。
2. **"动作"没有任何副作用**。`SceneEngine._parse_turn()` 只是用正则把
   `*动作*` 从原始回复中抠出来存进 `DialogueTurn.action` 字段，纯文本记录，
   不会更新任何角色状态、位置、物品，也不会让"环境"发生真正变化。
   `_scene_context()` 只是场景开始时的一份静态 dict（`config.initial_conditions`
   原样传入），场景运行全程不会因为角色的行动而改变，角色之间也无法感知
   彼此造成的"环境变化"。这与 CLAUDE.md 里"角色仅持有已知信息"的设计需要
   一个"世界会因为行动而演变"的机制来配合，目前完全空缺。

这两个问题共享同一个技术根源：**当前引擎里"发言"和"行动"都只是纯文本生成，
没有任何结构化的、可被程序读取并产生副作用的输出通道。**

## 2. 目标（Definition of Done）

### 2.1 Selector 模式完整化

文件：`backend/scene_engine/engine.py` 的 `_llm_select_speaker()`

- 让 LLM 返回结构化结果（如 JSON `{"character_id": "..."}` 或严格要求"只回复候选
  列表中的 `character_id`"），而不是自由文本名字匹配。候选列表用
  `character_id`（而非中文 `name`）做唯一匹配，避免同名/子串误判。
- 增加失败重试（复用 `backend/utils/llm.py` 里已有的 3 次重试机制即可，不需要
  重新实现），重试耗尽后的兜底策略需要**可观测**（记录 warning 日志，说明是
  兜底选择而非 LLM 主动选择），而不是静默选 `self.agents[0]`。
- 可选：支持 `SceneConfig` 里配置"是否允许同一角色连续发言"（当前实现没有这个
  限制，`selector` 模式下可能连续多轮选中同一角色）。
- 补充单元测试：mock `chat_safe` 返回值，覆盖"正常选中"“返回了不存在的
  character_id”“LLM 调用异常”三种场景，断言选择结果和日志行为。

### 2.2 动作解析与环境交互（WorldState 写回）

**前置条件**：需要工单07的 `WorldState` 数据模型已经落地
（`project_id`/`branch_id`/`variables: dict[str, Any]`）。若07未完成，本工单
可以先实现一个**场景内临时环境状态**（不持久化到 WorldState，只在
`SceneEngine` 实例内维护一个 `self._scene_env: dict` 贯穿本场景各轮次），
待07完成后再接入持久化。

- 在 `SceneEngine.run()` 的每轮循环里，`_parse_turn()` 解析出 `action` 文本后，
  新增一步"动作效果解析"：
  - **规则版（优先实现，成本低）**：维护一个可配置的关键词→变量映射表
    （例如"前往/走向/来到 + 地点词" → 更新该角色 `current_location`），
    命中则直接更新 `WorldState.variables` 或 `CharacterState.current_location`。
  - **LLM 辅助版（可选，成本更高）**：当规则版无法覆盖时，追加一次轻量 LLM
    调用，输入本轮 `action` 文本，要求输出结构化的"状态变更建议"
    （如 `{"character_id": ..., "field": "current_location", "value": ...}`），
    解析后应用。**必须做无害化校验**（只允许修改白名单字段，防止 LLM 输出
    污染无关状态）。
- 更新后的环境状态需要在下一轮 `_scene_context()` / 各角色 `build_system_prompt()`
  中体现（例如"当前你在哪里""其他角色当前位置"），让角色能感知到环境变化，
  形成真正的"环境交互"闭环。
- 可选联动工单06：若某次动作被判定为"重要事件"（复用工单07/记忆系统里已有的
  重要事件判定标准），额外调用 `GraphManager.add_entity`/`add_relation`
  同步写回 Kuzu 图谱，实现"模拟过程中动态更新知识图谱"（而不仅是 GraphRAG
  构建阶段一次性写入）。

### 2.3 技术选型结论（写死在本工单里，避免后来者重复纠结）

沿用 2026-07-15 讨论的结论：**默认自研（规则引擎 + 现有 `chat_safe` 直连调用），
不引入真实 AutoGen GroupChat/工具调用**。原因：
- 当前手写引擎已满足快照优先、每轮动态记忆注入等本项目特有约束，AutoGen 的
  `GroupChat` 只是重新实现这些编排逻辑，性价比低。
- 真正需要引入 AutoGen 的触发条件是"角色需要主动调用结构化工具函数
  （如 `move_to()`/`pick_up()`）"这种更复杂的场景，且现有正则/规则/轻量 LLM
  抽取方案已经无法覆盖时——届时再评估迁移到 `AssistantAgent` + `FunctionTool`，
  由框架接管工具调用循环。
- `backend/agents/base_agent.py` 的 `make_model_client()` 已经修复为支持传入
  `model` 参数（异构模型），作为该分支未来若被启用时的前置修复，但**目前
  仍是 dead code，未被 SceneEngine 调用**。

## 3. 涉及文件

- `backend/scene_engine/engine.py`（核心改动：`_llm_select_speaker`、新增动作
  效果解析步骤）
- `backend/models.py`（若07已完成：复用 `WorldState`；否则本工单内临时定义
  场景内环境 dict 的结构）
- `backend/knowledge_graph/graph_manager.py`（可选，联动06时使用现有
  `add_entity`/`add_relation`）
- `backend/scene_engine/scene_config.py` / `backend/models.py` 的 `SceneConfig`
  （若新增"是否允许连续发言"等配置项）
- `tests/test_scene_engine.py`（新增 Selector 结构化选择、动作效果解析的单测）

## 4. 预估工作量（粗略估计，非精确值）

| 子项 | 开发工作量 | 备注 |
|---|---|---|
| Selector 结构化改造 | 0.5-1 人天 | 改动集中在一个函数 + 单测，token 开销基本不变（仍是每轮一次调用，只是要求输出格式变化） |
| 动作解析——规则版 | 1-2 人天 | 纯本地逻辑，无额外 LLM 调用，token 零增量 |
| 动作解析——LLM 辅助版（可选） | 1-2 人天 | 每轮若命中"动作非空"才触发额外调用，粗估单次调用 200-500 input token + 50-150 output token；20 轮场景中若每轮都有动作，约新增 5,000-13,000 token/场景（可通过"仅动作非空才调用"显著降低命中率，实际通常远低于此上限） |
| 联动06图谱回写（可选） | 1-2 人天 | 依赖 `EntityExtractor.extract()` 是否为 LLM 实现，若是则每次重要事件额外增加一次抽取调用的 token 开销，量级与上一行相近 |
| **合计** | **约 3-5 人天**（不含图谱联动）**/ 5-8 人天**（含图谱联动） | 以上均为粗略估计，实际耗时取决于 WorldState（工单07）是否已就绪、规则表覆盖度要求、以及是否要求 LLM 辅助版 |

> 注：token 预估基于"每轮一次额外调用、prompt 在几百 token 量级"的保守假设，
> 实际数值会随 prompt 设计、模型上下文窗口利用率、场景轮次数浮动，仅供排期参考，
> 不作为成本承诺。

## 5. 验收方式

1. 构造一个 3 人以上角色的场景，`speaker_mode=selector`，跑 10+ 轮，人工检查
   发言分布是否合理（不应出现"LLM 返回了不存在的角色名却被误匹配"的情况），
   日志中兜底选择要能被明确观察到（而非静默发生）。
2. 构造一个包含明显位置变化动作的场景（如"角色说'我要去码头'"），验证下一轮
   该角色的 `current_location`（或环境 dict 对应字段）确实更新，且其他角色的
   system prompt / 场景上下文能看到这个变化。
3. 若启用图谱联动：验证场景结束后 Kuzu 图谱中出现了模拟过程中产生的新增
   实体/关系，而不仅是 GraphRAG 构建阶段的静态内容。
