# 工单28：项目叙事目标持久化 + 结局判定

**优先级**：P1
**预估改动范围**：中（动数据模型，走 CLAUDE.md §5.4 三步 checklist）
**依赖**：04（`evaluate_scene` 的签名扩展在 04 里做）

---

## 1. 背景

`narrative_goal` **是一个只活了一次 HTTP 请求的字符串参数，从来不是系统状态**：

```
用户输入 → PlanSceneRequest → orchestrator.plan_scene → _PLAN_PROMPT.format → LLM → SceneConfig
                                                                                  ↓
                                                                          函数返回即消失
```

由此产生四个断点：

| 断点 | 位置 | 后果 |
|---|---|---|
| 不持久化 | `Project` 无字段，`Scene`/`SceneConfig` 也不带 | 目标生命周期 = 一次请求 |
| 自动流程里被替换 | `orchestrator.apply_decision` 的 next_scene 分支用 `f"延续上一场（{scene.name}）的剧情走向"` | 连跑 N 场后系统只剩**动量**没有**引力**，这是开环不是导向 |
| 评估无对标 | `_EVAL_PROMPT` 只有 `scene.description` | `plot_deviation_score`（偏离主线）连基线都没有，纯猜 |
| 无结局判定 | 全链路 | 只有 `max_turns` / 停滞两个机械终止条件，没人回答"故事讲完了没有" |

CLAUDE.md §1.4 的设计信条是"导演宏观、角色微观"，而导演现在手里根本没有那个"宏观"。

## 2. 目的

给导演立一个**只读的锚点**（故事目标）并配上**可观测的度量**（推进度 / 未收束线索 / 结局判定），
使"朝目标宏观导向"从口号变成可执行、可验收的东西。

这是三层目标模型的第一层：

| 层 | 载体 | 谁能改 | 归属工单 |
|---|---|---|---|
| **故事目标** | `Project.narrative_goal` / `ending_criteria` | **只有用户** | **本单** |
| 路线图 | `Storyboard.outline` | 导演可写，留痕 | 18 |
| 场次意图 | `SceneConfig.scene_goal` | 导演生成，用户可覆盖 | 18 |

## 3. 目标（Definition of Done）

### 3.1 数据模型（严格走 §5.4 三步）

1. `backend/models.py`：
   ```python
   class Project:
       narrative_goal: str = ""      # 用户设定的主线目标 / 期望结局
       ending_criteria: str = ""     # 可选：结局判定标准（自然语言）

   class SceneEvaluation:
       story_progress: float = 0.0            # 0-1，主线推进度
       is_ending_reached: bool = False
       ending_reason: str = ""
       unresolved_threads: list[str] = []     # 未收束的线索
   ```
2. `backend/services/repository.py`：`_deserialize_project` 与 `_deserialize_evaluation`
   补上新字段 —— **漏这步字段会静默丢失**。
3. 不加 SQL 列（不需要索引/过滤/CAS，`data_json` 自动携带）。
4. 同步 `frontend/src/types/index.ts`。

### 3.2 链路打通（四处）

| 位置 | 改动 |
|---|---|
| `POST /projects` | `CreateProjectRequest` 增 `narrative_goal` / `ending_criteria` |
| `orchestrator.plan_scene` | 传入的 goal 为空时**回退读 `project.narrative_goal`**；`PlanSceneRequest.narrative_goal` 改为可选 |
| `orchestrator.apply_decision`（next_scene） | 删掉 `f"延续上一场…"`。goal 用主线目标，"上一场的结果"作为**独立上下文块**传给导演，不再冒充目标 |
| `DirectorAgent` 两个 prompt | 新增固定的【主线目标】块；`_EVAL_PROMPT` 输出增加 §3.1 的四个新字段 |

### 3.3 度量的两条实现约束

1. **`story_progress` 单调钳制**：LLM 自评噪声大，新值低于历史值时不回退，只累加"停滞"信号。
   否则进度条来回抖，基于它的决策规则会跟着抖。
2. **`unresolved_threads` 上限 20 条**，按最近提及排序。否则长线推演会把导演上下文吃光。

### 3.4 结局判定的归属

判定放在**评估阶段**（导演看完这场后回答），**不放 `backend/scene_engine/termination.py`**：

- `termination.py` 是**场景内**终止（够不够轮次、停不停滞），保持纯本地零 LLM；
- 结局是**故事级**判断，需要主线目标 + 全部历史，属于导演职责。

前端：`is_ending_reached=True` 时 `DirectorPanel` 主按钮换成"生成结局输出"，跳 `Output.vue`。

## 4. 明确不做

- **不新增 `DecisionType.END` 枚举**。会波及决策 CAS、`decisions` 幂等表、前端三处，
  收益只是一个标志位。用 `is_ending_reached` 表达。
- **不允许导演改写 `narrative_goal`**。这是设计红线：自评系统的经典失效模式是模型把目标
  改成自己刚演出来的东西，然后分数变高。锚点只读，才谈得上宏观导向。
- 不做多目标 / 目标树。等工单 22 真的要在树上比较节点价值时再说。

## 5. 涉及文件

- `backend/models.py`、`backend/services/repository.py`
- `backend/api/schemas.py`、`backend/api/projects.py`
- `backend/services/orchestrator.py`（`plan_scene` / `apply_decision`）
- `backend/agents/director_agent.py`
- `frontend/src/types/index.ts`、`pages/Workspace.vue`、`components/DirectorPanel.vue`
- CLAUDE.md §4 数据模型、§8 API

## 6. 验收方式

1. **老项目兼容**：`data/projects/` 下已存在的项目（其 `data_json` 无新字段）能正常
   `GET /projects` 与 `GET /projects/{id}`，`narrative_goal` 为空串而非报错。
2. **目标不漂移**：建一个带明确目标的项目，连跑 3 场（每场都走 next_scene 决策），
   检查第 3 场的规划 prompt 里仍然包含原始主线目标，而不是"延续上一场"。
3. **偏离度有意义**：人为构造一场完全跑题的对白，`plot_deviation_score` 应显著升高
   （改动前它对此无感知）。
4. `story_progress` 在连续两次评估中给出更低值时不回退。
