# 工单04：补全导演评估上下文 + 实现 query_character_state

**优先级**：P1
**预估改动范围**：小-中（1 个核心文件）
**依赖**：建议在工单**14** 之后做（工单Է5 提供了从快照回填运行时记忆的机制，可升级下文 §3.2 的折中方案）；
工单**05** 建议在本工单之后做，可复用这里实现的 `query_character_state`

---

## 0. 状态更新（2026-09-03，开工前重新核对代码）

**本单范围已变化，请以本节为准：**

| 原章节 | 状态 |
|---|---|
| §2.2 / §3.2 `query_character_state` 空壳 | ✅ **已随工单17 落地**到 `backend/services/inspection.py`，本单不再涉及（下文保留仅供追溯） |
| §2.1 / §3.1 评估上下文缺失 | ⬜ 仍然有效，本单核心 |
| §2.3 / §3.3 `plan_scene` 角色描述过简 | ⬜ 仍然有效 |
| **§6（新增）** | ⬜ 本轮复核发现的 5 个新缺陷，一并在本单修掉 |

本单的目的一句话：**让导演不再瞎评**。它现在评一场戏时，既不知道角色是谁，
也没有任何上下文预算，解析失败还会伪装成一份正常评估。

本单**不涉及**主线目标（工单 28）与导演长期记忆（工单 18），也**不要**顺手修
`query_graph` / `GraphManager` 未 `connect()`——那是 CLAUDE.md §12.2 登记的 dead code，归工单 06。

**前置**：工单 27-A（统一上下文压缩管线）。下文 §6 的 D4 直接消费它。

---

## 1. 背景

`DirectorAgent` 按设计应该是"唯一拥有全局知识图谱访问权、可查看所有角色内部状态"的实体
（见 `CLAUDE.md` 5.3 节），但实际实现中：
1. `evaluate_scene` 评估场景时**完全没有把角色卡信息传给 LLM**，导致"角色一致性评分"这项打分
   本质上是 LLM 瞎猜（它连角色设定长什么样都不知道）。
2. `query_character_state` 是一个空壳方法，从未真正查询任何数据。
3. `plan_scene` 里的角色描述只有 `persona[:80]` 的粗暴截断，信息量很少。

## 2. 精确错误位置

文件：`backend/agents/director_agent.py`

### 2.1 评估 prompt 缺角色信息

```python
_EVAL_PROMPT = """你是一位影视导演，正在评估刚刚模拟完的场景。

【场景预设目标】
{description}

【场景对白记录】
{transcript}

请客观评估并严格输出 JSON（不要额外文字）：
{{
  "synopsis": "场景梗概（50-100字）",
  "narrative_goal_score": 0-10,
  "dramatic_tension_score": 0-10,
  "plot_deviation_score": 0-10,
  "character_consistency_score": 0-10,   # <-- 问题：没有任何角色设定信息作为评分依据
  "recommended_decision": "continue|next_scene|rollback",
  "rollback_reason": "若建议回滚，说明原因，否则空字符串"
}}
"""
```

```python
async def evaluate_scene(
    self,
    scene: Scene,
    dialogue_log: list[DialogueTurn],
) -> SceneEvaluation:
    transcript = self._format_transcript(dialogue_log)
    prompt = _EVAL_PROMPT.format(description=scene.description, transcript=transcript)
    # ↑ 问题：只传了 scene.description 和 transcript，完全没有传入参与角色的
    #   persona / known_facts / relationships，评分 "character_consistency_score" 没有依据
    raw = await chat_safe(...)
```

调用方 `backend/services/orchestrator.py` 的 `run_scene` 函数中调用 `director.evaluate_scene(scene, result.dialogue_log)`，
同样没有传入角色卡列表——这个信息需要一路传进来。

### 2.2 query_character_state 空壳

```python
async def query_character_state(self, character_id: str) -> CharacterState:
    return CharacterState(character_id=character_id)
    # ↑ 问题：只返回一个几乎全默认值的空对象，没有真正查询 repository 或 memory
```

### 2.3 plan_scene 角色信息过于精简（次要问题，建议顺手改）

```python
char_desc = "\n".join(
    f"- {c.name}：{(c.persona or '')[:80]}（目标：{c.current_goal}）"
    for c in available_characters
)
```
只截取 persona 前 80 字，且完全不包含 `known_facts`/`relationships`，导演规划场景时对角色的
了解非常有限。

## 3. 目标（Definition of Done）

### 3.1 修复 evaluate_scene（核心）

1. 修改 `evaluate_scene` 方法签名，新增参数 `characters: list[CharacterCard]`（参与本场的角色卡列表）。
2. 修改 `_EVAL_PROMPT`，新增一个 `{character_profiles}` 占位块，格式建议：
   ```
   【参与角色设定】
   - 张三：性格...（说话风格：...）已知事实：[...]
   - 李四：...
   ```
3. 修改调用方 `backend/services/orchestrator.py` 的 `run_scene` 函数：在调用
   `director.evaluate_scene(...)` 前，用 `agents` 列表（已经在函数里构建好了）取出各自的 `.card`
   传进去，即 `director.evaluate_scene(scene, result.dialogue_log, [a.card for a in agents])`。
4. 确保**不要把 `unknown_facts` 排除**——导演是唯一可以看到 `unknown_facts` 的角色，这里传入完整
   角色卡是符合 CLAUDE.md 设计的（区别于 `CharacterAgent.build_system_prompt` 绝对不能注入
   `unknown_facts` 的约束，那是角色自己看不到，但导演可以看到）。

### 3.2 实现 query_character_state（✅ 已随工单17 完成，本单不再涉及，以下仅供追溯）

1. 修改方法签名，需要能访问 `repository`（当前 `DirectorAgent` 构造时没有传 `project_id` 对应的
   repository 访问方式，但 `self.project_id` 已存在，可以直接 `from backend.services import repository`
   然后调用 `await repository.get_character(self.project_id, character_id)`）。
2. 同时应该聚合该角色的运行时记忆状态：构造一个 `MemoryManager(character_id, self.project_id)`，
   调用其 `short_term.dump()` 和 `episodic.dump()`（若已 connect；不需要 `connect()` 长期记忆连接，
   短期/事件摘要是内存态，需要注意这里若 `MemoryManager` 是新建实例，`short_term`/`episodic` 会是空的——
   这是本项目当前"运行时状态"与"持久化状态"脱节的已知限制。**折中方案**：至少把
   `CharacterCard` 里已持久化的 `current_emotion/current_goal/current_location/relationships` 正确填入
   返回的 `CharacterState`，这是可以立即拿到的准确数据；记忆部分若拿不到运行时内存态，可以先留空
   或标注 TODO，不强制在本工单内解决"跨进程共享运行时记忆"这个更复杂的问题）。

   > **更新（工单 14 完成后）**：上述"记忆部分留空"的折中方案已可以升级。工单 14 在
   > `backend/services/orchestrator.py` 实现了 `_load_inherited_states(scene, sm)`（按
   > `snapshot_id_after` → `restore_snapshot_id` → `snapshot_id_before` → 父场景快照的优先级
   > 从快照读出 `CharacterState`），并给 `MemoryManager` 增加了 `prime(short_term_buffer,
   > episodic_summary)` 回填接口。本工单的 `query_character_state` 应直接复用这套机制：
   > 从该角色最近一个快照里取 `short_term_buffer` / `episodic_summary` 填入返回值，
   > 而不是新建一个恒为空的 `MemoryManager`。同样的修正也适用于
   > `backend/api/characters.py` 的 `GET /characters/{char_id}/memory` 接口（它目前恒返回空，
   > 是工单 05 记忆面板的前置依赖）。
3. 返回值示例：
   ```python
   async def query_character_state(self, character_id: str) -> CharacterState:
       from backend.services import repository
       card = await repository.get_character(self.project_id, character_id)
       return CharacterState(
           character_id=character_id,
           current_emotion=card.current_emotion,
           current_goal=card.current_goal,
           current_location=card.current_location,
           relationships=dict(card.relationships),
       )
   ```

### 3.3 增强 plan_scene 角色描述（次要，建议一并做）

将 `char_desc` 拼接逻辑改为包含 `known_facts`（取前 2-3 条）和主要 `relationships` 摘要，
参考 `CharacterAgent._relationship_summary()` 的写法（可以复用类似逻辑，不必完全一致）。

## 4. 涉及文件

- `backend/agents/director_agent.py`（核心改动：`_EVAL_PROMPT`、`evaluate_scene`、
  `query_character_state`、`plan_scene`）
- `backend/services/orchestrator.py`（`run_scene` 函数中调用 `evaluate_scene` 的地方，传入角色卡列表）

## 5. 验收方式

1. 跑一个场景并触发评估，检查发给 LLM 的实际 prompt（可临时加日志打印或用调试断点）确认
   `character_profiles` 确实被正确填充了角色设定信息。
2. 调用 `director.query_character_state(some_character_id)`（可写一个简单脚本或临时测试），
   确认返回的 `CharacterState` 中 `current_emotion`/`current_goal` 等字段与该角色卡 JSON 文件中的
   实际值一致，而不是默认值 "平静"/""。
3. 如有条件，人工比较修改前后同一场景的 `character_consistency_score` 差异，确认评分不再是"空转"。

---

## 6. 【新增】本轮复核发现的缺陷（一并修掉）

| 编号 | 问题 | 位置 | 要求 |
|---|---|---|---|
| **D1** | **AI 规划的 `opening_narration` 被静默丢弃** | `orchestrator.create_scene_from_config` | 引擎读的是 `scene.initial_conditions["opening_narration"]`，而这里只搬了 `config.initial_conditions`，同名字段没搬。手动建场景路径（`api/scenes.py`）有写入，**只有 `next_scene` 决策这条自动路径丢**——即导演自己写的开场白永远不生效。修复时留一行注释说明 `initial_conditions` 才是权威载体 |
| **D2** | **JSON 解析失败伪装成正常评估** | `_extract_json` → `evaluate_scene` | 现在返回 `{}` → 四项分数全默认 5.0、synopsis 空、推荐 next_scene，**无任何告警**。改为：`_extract_json` 返回 `(data, ok)`；失败时 `logger.warning` 打出原文前 300 字，分数置 **-1**（不是 0，也不是 5），synopsis 写明解析失败，前端据此显示"评估失败"而非一份看起来正常的低分 |
| **D3** | **角色名→ID 映射失败静默兜底** | `plan_scene` | LLM 返回别名/重名时 `chosen_ids` 为空 → 直接取 `available_characters[:2]`（即实体抽取顺序的前两个），不记日志。改为：精确 → 去空格 → 最长子串包含 三级匹配；未命中的名字 `logger.warning`；兜底改为按最近出场频次取 2–3 人 |
| **D4** | **导演侧完全没有上下文预算** | `_format_transcript` | 全量 transcript（含所有 `inner_thought`）直接进 prompt，而 `max_turns` 无上限校验。接入工单 27-A 的 `compact_lines`，策略 `llm_summary`，预算 `DIRECTOR_TRANSCRIPT_BUDGET`。**绝不用尾部截断**——评估最需要的就是结局 |
| **D15** | 规划与评估共用 `temperature=0.3` | `DirectorAgent.__init__` | 规划要创意、评估要一致，同温度是场景同质化的来源之一。拆为 `DIRECTOR_PLAN_TEMPERATURE=0.7` / `DIRECTOR_EVAL_TEMPERATURE=0.3` / `DIRECTOR_DECISION_TEMPERATURE=0.1`，构造参数保留为覆盖入口 |
| **D16** | 导演是唯一没有测试的 agent | — | 新建 `tests/test_director_agent.py` |

### 6.1 契约提醒

- **`unknown_facts` 进导演 prompt 是契约1 的合法例外**（导演有全知权，且这些内容不进入任何
  角色可见上下文）。请在代码里留一行注释写明，否则下一个人看到会当 bug 顺手删掉。
- `_extract_json` 改签名后**必须全量搜调用点**（`plan_scene` 与 `evaluate_scene` 都在用），
  漏一处就是把 tuple 当 dict 用。

### 6.2 新增验收

`tests/test_director_agent.py`（mock `chat_safe`）至少覆盖：

1. 评估 prompt 内确实出现参演角色名与其 `unknown_facts` 内容；
2. LLM 返回非法 JSON 时不抛异常，分数为 -1，且打了 warning；
3. 角色名带别名时能匹配上；完全不匹配时走频次兜底并 warning；
4. `plan_scene` → `create_scene_from_config` 后 `Scene.initial_conditions["opening_narration"]` 非空（D1 端到端）；
5. 超长 transcript 触发压缩后，结果仍包含**最后一轮**对白。

### 6.3 涉及文件（在 §4 基础上追加）

- `backend/config.py`（三个温度配置）、`.env.example`
- `frontend/src/components/DirectorPanel.vue`（-1 分显示为"评估失败"）
- `tests/test_director_agent.py`（新建）
- CLAUDE.md §4.2 增补一条陷阱：`opening_narration` 的权威载体是 `initial_conditions`，
  `SceneConfig` 的同名字段只是规划期载体，**新增建场景路径必须搬运**
