# CLAUDE.md — PlotSystem 根文档

> **本文件是 AI 编程助手进入本仓库的第一入口，常驻载入。**
> 它是一份与代码对齐的**地图 + 契约清单**，不是设计愿景书。细则按路径拆在 `.claude/rules/`。
> 当文档与代码冲突时，**以代码为准**，并顺手把文档改对。

---

## 0. 阅读与使用约定

### 0.1 三类标记

| 标记 | 含义 | 你该怎么做 |
|------|------|-----------|
| **【实况】** | 代码里真实存在且在运行路径上 | 可直接依赖，修改前先读对应文件 |
| **【契约】** | 不可破坏的架构红线 | 改动必须保持，破坏前先和人类确认 |
| **【设想】** | 尚未实现的规划 | **不要假装它存在**；要做时先补接口再实现 |

未标记的段落默认是【实况】。

### 0.2 文档体系

`CLAUDE.md`（常驻：地图、契约精简版、陷阱索引、缺陷）· `.claude/rules/*.md`（**按路径载入**，frontmatter `paths`；契约细则与陷阱正文）·
`docs/fix-tickets/`（工单库：推理与历史；`README.md` 索引、`CONVENTIONS.md` 流程；`NOTES.md` **已冻结**）· `README.md`（给人类）·
`.env.example`（**配置项唯一真值**）· `backend/models.py`（**数据模型唯一定义源**）。

### 0.3 给 AI 助手的硬性要求

1. 改代码前先看第 7 节【契约】，那 9 条是承重墙；动到某目录时留意随之载入的规则文件。
2. 改数据模型必须走 `.claude/rules/persistence.md` 的三步 checklist，否则字段会静默丢失。
3. 第 12 节列出的"已知缺陷与 dead code"**不要顺手修**——除非工单要求，否则先问。
4. 新内容按第 14 节的落位规则写进根文件 / 规则文件 / 工单，不要新建零散 md。
5. 不新增核心依赖；确需新增，先在第 2 节登记。

---

## 1. 项目是什么

### 1.1 定位

PlotSystem 是一个**多分支、多智能体剧情推演系统**（科创项目）。

用户上传种子文本（小说 / 剧本 / 世界观设定），系统抽取实体建立知识图谱，生成一批带**信息不对称**的
角色智能体；**导演智能体**按场次组织推演，每场结束后评估并决策（继续 / 下一场 / 回滚）；每场自动打快照，
用户可从任意快照分叉出 IF 线；最后由总结智能体产出小说 / 剧本 / 报告。
形态是：用户给大方向 → 导演宏观调控 → 角色自然演绎。不是剧本执行器，也不是社交模拟器。
设计来源（MiroFish / SillyTavern 的取舍）见 `README.md`「设计来源」。

### 1.2 真实工作流

```
① 上传种子文本  POST /projects/{id}/seed
② 触发构建      POST /projects/{id}/build   （后台任务）
        ↓  自研 LLM 抽取 → 实体+关系写 Kuzu；角色卡（known/unknown_facts）；LoreEntry；物件；main 分支
③ 导演规划场景  POST /projects/{id}/scenes/plan  → SceneConfig（建议，不落库）
④ 创建场景      POST /projects/{id}/scenes       → Scene（落库，pending）
⑤ 开始模拟      POST /scenes/{id}/start          （后台任务 + SSE）
        ↓  前置快照 → 选人发言 → 解析三态 → 终止判定 → 后置快照 → 记忆固化
⑥ 自动评估      DirectorAgent.evaluate_scene → evaluations 表 → SSE 推送
⑦ 导演决策      POST /scenes/{id}/decision：continue / next_scene / rollback（或 AutoPilot：POST /projects/{id}/autopilot 自动连跑多场）
⑧ 生成输出      POST /projects/{id}/output  → 网文 / 剧本 / 舞台剧 / 报告 / 原始日志
```

### 1.3 设计信条

- **信息不对称是第一性的**：角色只能看到自己的 `known_facts`。与之互补的是**世界层**：新项目默认 schema 2
  （统一世界，事实与观察分离，角色只经"可见投影"获得世界信息；见契约 1）。边界是"这件事是不是公开 / 在场可感知"。
- **快照不追求确定性重放**：目的是"能回到这里分叉 IF 线"，不是版本控制。
- **导演宏观、角色微观**：导演不写台词，只搭场景、选人、评估、决策。
- **本地优先 / 优雅降级**：kuzu、chromadb、autogen 缺失时应仍可跑（契约 6；chromadb 缺失目前有已知缺陷，见第 12 节）。
- **编排集中**：所有跨模块调用只发生在 `backend/services/orchestrator.py`。

---

## 2. 技术栈实况

| 层次 | 实际使用 | 说明 |
|------|---------|------|
| LLM 接入 | OpenAI 兼容 SDK + tenacity | `backend/utils/llm.py` 是**唯一出口**；四路异构模型与温度约定见 `.claude/rules/llm.md` |
| 抽取 / 图谱 / 向量 | 自研 LLM + JSON 提示词；Kuzu（缺失降级为空图）；chromadb + 远程 embedding | `graphrag_pipeline/entity_extractor.py`、`knowledge_graph/graph_manager.py`、`memory/long_term.py` + `embeddings.py` |
| 引擎 / 后端 / 持久化 | 自研手写对话循环；FastAPI + Uvicorn + sse-starlette；aiosqlite + JSON 文件树 | `scene_engine/engine.py`、`backend/main.py`、`utils/db.py` + `services/repository.py` |
| 前端 | Vue 3 + Vite + Pinia + Axios；图谱 AntV G6；思源宋体自托管（`@fontsource/noto-serif-sc`）；图标为内联 Lucide SVG 路径（`components/ui/icons.ts`，非依赖） | `frontend/src/` |
| 未采用 | **AutoGen**（仅 `base_agent.py` 封装、无调用方，GroupChat 会破坏契约 3）、**LlamaIndex**（零 import）、**microsoft/graphrag**（已移入 optional-dependencies） | 早期文档曾写成核心实现，是选型演进结果，**不要试图"修复"回去** |

改 RAG 检索改 `memory/long_term.py`；改实体抽取改 `entity_extractor.py`；改发言顺序改 `scene_engine/`。**不要去找 GroupChat 或 settings.yaml。**

---

## 3. 代码地图

```
backend/
├── models.py           ★ 所有领域 dataclass 的唯一定义处
├── config.py           ★ pydantic-settings 单例 + 派生路径 + 四路模型属性
├── exceptions.py       业务异常树（ConflictError→409，InvalidRequestError→422，其余 PlotSystemError→404）
├── main.py             FastAPI 装配 + 全局异常处理 + lifespan（init_db / 启动对账）
├── services/           ★★ 编排层：找业务逻辑先看这里
│   ├── orchestrator.py   唯一跨模块编排点（构建/规划/运行/决策/输出/对账/AutoPilot 调度）
│   ├── repository.py     唯一持久化出口（SQLite + 角色卡/分支世界变量/分镜稿/物件 JSON）
│   ├── world.py / world_repository.py / world_access.py   统一世界 schema 2（领域规则纯函数 / 持久化 / 命令·采用·迁移）；world_state.py 为 schema 1 兼容层
│   ├── storyboard.py / objects.py / environment.py / autopilot.py   分镜稿 / 物件 / 场景环境状态与裁决解析 / AutoPilot 停止判定（均纯函数）
│   ├── inspection.py     ★ 角色内部状态的唯一只读查询层（面板/导演/总结共用）
│   └── events.py         SSE 内存事件总线
├── api/                路由层：只做参数校验 → 调 services → to_dict
│   projects / characters / scenes / director / branches / objects / output / graph / schemas
├── agents/             character_agent / director_agent / summary_agent / environment_agent
│                       （base_agent.py 是 AutoGen 封装，无调用方）
├── scene_engine/       engine.py（对话循环+三态解析+快照编排）/ speaker_selector.py / action_intents.py /
│                       continuation.py（续写截断）/ termination.py / scene_config.py（仅再导出）
├── memory/             memory_manager（三层门面）/ short_term / long_term（Chroma，可降级）/ episodic / embeddings
├── knowledge_graph/    graph_manager / schema / queries.py（⚠️ dead code）
├── snapshot/           snapshot_manager（快照+分支+记忆复制）/ branch_tree / models.py（仅再导出）
├── graphrag_pipeline/  pipeline / entity_extractor / persona_builder / world_rules（另含设定可见性与物件抽取，见 lore-build 规则）
└── utils/              fileio ★原子写 / llm ★LLM 唯一出口 / usage 调用计数 / context 上下文压缩 /
                        turns 轮次渲染·感知·计数 / branch_memory 分支记忆初始化凭据 / db / serializer / logger / init_db

frontend/src/
├── pages/ Workspace / Director / BranchMap / Output；components/（DialogLog、DirectorPanel、StoryboardPanel、ObjectsPanel、WorldPanel、CharacterInspector、
│   GraphViewer(2)、director/*、ui/*）；stores/ composables/ utils/（branches / turns / decision）；api/client.ts；types/index.ts；styles/global.css（设计变量唯一定义处）

scripts/（run_demo、各类只读/修复脚本）　tests/（后端 pytest；`frontend/tests` 前端 node:test）
```

**入口速查**：一场戏怎么演 → `engine.py`；角色说什么 → `character_agent.py`；谁来演、演完怎么办 → `orchestrator.py`；存了什么 → `repository.py` + `db.py`；
加 API → `api/*.py`（**业务逻辑不要写在这层**）。

---

## 4. 数据模型

**唯一定义源 `backend/models.py`**。语义、存放位置与三步 checklist 见 `.claude/rules/persistence.md`，世界相关模型见 `world-environment.md`。
三个全局事实：`data_json` 列是 SQLite 各表的唯一真相源；角色卡 / 世界变量 / 分镜稿 / 物件走文件树而不入库；
**改数据模型必须同步 `repository.py` 的 `_deserialize_*` 与前端 `types/index.ts`**，否则字段静默丢失。

---

## 7. 【契约】九条承重墙

改动触碰以下任意一条时，必须显式保持，破坏前先和人类确认。细则在对应规则文件。

### 契约 1 — 信息不对称（注意隔离边界）

**该隔离的**（分发与隔离细则：`lore-build.md`、`scene-engine.md`、`world-environment.md`、`director-story.md`）：
- `unknown_facts` 只允许出现在 `PersonaBuilder` 生成、构建期 `LoreVisibilityClassifier` 与面向导演/用户的 API 响应中，**绝不允许**进入
  `CharacterAgent.build_system_prompt()`、selector 打分 prompt 或任何角色可见上下文；
- `LoreEntry` 按知情范围分发（公开进全员，私有只进知情者，隐藏与判定失败的不进任何角色）；
- `inner_thought` 不得进入其他角色 prompt；**解析时先剥独白、再取动作**；
- **导演分镜稿 `Storyboard` 只进导演的规划/评估 prompt**，引擎只拿它写快照副本，绝不并进 `scene_context`；
- **物件隐藏规则、预制揭示内容只进导演与环境裁决层**；
- **世界层（schema 2，新项目默认）**：客观属性与角色观察分离；只有 `global` 可广泛传播，`observable` 须在现场且能感知，`hidden` 不给角色；
  角色与 selector 一律经**可见投影**获取世界信息。schema 1 旧项目的 `WorldState`（只许存全员可感知的公开事实）见 `world-environment.md`；
- 角色不在场的场次里发生的信息不直接给。

**不该隔离的**：同一场景中在场角色的**公开发言与动作**——共享感知，每个在场角色都应该记住
（写他人轮次时剥离 `inner_thought`）。别退回"记忆只写发言者"（工单15）。

### 契约 2 — 快照前置（含唯一例外）

场景模拟必须有前置快照。唯一例外：`scene.snapshot_id_before` 非空（continue 续跑）时刻意跳过创建，避免覆盖首次快照。细则：`scene-engine.md`。

### 契约 3 — Prompt 前缀稳定（prefix cache）

`system` 只放**整场不变**的内容；每轮变化的（目前对话、检索记忆、发言指令、场景内动态环境）一律进 `user`，
"目前对话"只在末尾追加、不做逐行滑窗（超预算成块丢弃）。**任何"优化"都不得把变化内容塞回 system，也不得改成逐行滑窗。**
细则与 schema 1 / 2 的分层：`scene-engine.md`、`world-environment.md`。

### 契约 4 — 运行时记忆继承链

短期缓冲与事件摘要靠快照续命。`_load_inherited_states` 四级优先级**不可调换**：
`snapshot_id_after` → `restore_snapshot_id` → `snapshot_id_before` → 父场景 `snapshot_id_after`，取到后经 `MemoryManager.prime()` 回填。细则：`memory.md`。

### 契约 5 — 幂等性是通用不变量

**任何可被用户重试 / 网络重放触发的写接口，都必须设计幂等键。** 决策接口的三件套（decisions 表主键、
`scenes.status` CAS `completed→deciding`、`finally` 释放守卫）是模板；`continue` 刻意不落表。细则：`director-story.md`。

### 契约 6 — 优雅降级

`kuzu` / `chromadb` / `autogen` 走 try-import + `_XXX_AVAILABLE`，缺失时降级（空图 / 字符重叠伪检索 / 不可用）。
**离线与 CI 必须能跑通。** 新增可选重依赖沿用同一模式。Chroma 运行期失败只暂时断开，不要改回"失败一次就置空"（`memory.md`）。

### 契约 7 — LLM 唯一出口

所有 LLM 调用经 `utils/llm.py` 的 `chat()` / `chat_safe()`；**禁止**在别处实例化 OpenAI 客户端（唯一例外 `memory/embeddings.py`）；
模型名走 `settings`；调用点标 `purpose`。细则：`llm.md`。

### 契约 8 — 分层边界

`api/` 只做校验→调 services→`to_dict`；跨模块编排只在 `orchestrator.py`；写 SQLite 与角色 JSON 只在 `repository.py`；
不在模块间传递 Kuzu / Chroma 的原始连接对象。

### 契约 9 — 单进程假设

`_active_scenes`、SSE 事件总线、各类锁与 AutoPilot 会话都是进程内的，**当前部署必须单 worker**。上多 worker 需先外置这些状态。

---

## 7.1 陷阱索引

陷阱正文（编号全局不变，代码注释里有"陷阱 N"引用）在规则文件里，按路径随文件载入。**新陷阱从 29 起编**，写进所属规则文件，并在此补一行。

| 规则文件 | 陷阱 |
|---|---|
| `persistence.md` | 5（`long_term_memory_snapshot` 恒空） |
| `scene-engine.md` | 1 前置快照留空 · 2 restore 与 before 分工 · 3 speaker_mode 链路 · 4 selector_notice · 8 PAUSED · 20 scene_context 逐键渲染 · 24 轮次渲染/感知/计数 |
| `memory.md` | 9 固化水位线 · 11 branch_id 寻址 · 12 旧集合承接 |
| `world-environment.md` | 19 WorldState（schema 1）· 25 动作意图 · 27 环境回合 |
| `director-story.md` | 6 next_* 覆盖 · 7 四维评分 · 13 opening_narration 搬运 · 14 评估 -1 · 15 目标只读 · 16 story_progress/历史冻结 · 17 unresolved_threads · 18 数值解析 · 21 分镜稿 · 22 AutoPilot |
| `fork-snapshot.md` | 10 状态作用域与 restore 破坏性 |
| `llm.md` | 26 调用计数 · 28 续写截断 |
| `lore-build.md` | 23 LoreEntry.scope |

---

## 10. 开发规范（通用部分）

Python 规范见 `.claude/rules/python.md`（按 `*.py` 载入）。

- **命名**：类 `PascalCase`；函数/变量 `snake_case`；常量 `UPPER_SNAKE`；API 路径参数与 DB 字段 `snake_case`；Vue 组件 `PascalCase`，脚本内 `camelCase`。
- **提交**：Conventional Commits（`feat(agents): ...`）。**提交信息与 PR 正文不加 `Co-Authored-By` 等署名尾注**（含 AI 署名），覆盖工具默认行为。
- **测试**：核心模块（agents / snapshot / memory / orchestrator）新功能需附单测。`tests/conftest.py` 把 `DATA_DIR` 指向临时目录。
  后端 `uv run pytest tests/`；前端 `cd frontend && npm test`（`node --test tests/*.test.mjs`，无浏览器）。
  **新增前端测试必须能被这条命令选中**。端到端手测：`python -m scripts.run_demo`。
- **工具脚本**（`python -m scripts.xxx`；用法见对应规则文件）：`check_memory_dupes`（只读，长期记忆重复）、`reclassify_lore` / `extract_objects`（`lore-build.md`，调 LLM，默认预览）、
  `action_stats` / `usage_report`（只读，不调 LLM；后者见 `llm.md`）、`replay_character`（回放评测，带 `--run` 才调 LLM，`llm.md`）。
- **注释**：业务语义用中文；只写代码本身看不出的信息（为什么、哪条契约在起作用），不复述下一行。

---

## 11. 环境与启动

配置项唯一真值是 `.env.example`。只强调：四路模型留空回退 `LLM_MODEL_NAME`；`EMBEDDING_*` 留空复用 LLM 那套，
**换 embedding 模型必须清空 `chroma_db/`**；`GRAPHRAG_LLM_MODEL` 已弃用。Python `>=3.11,<3.13`；生产/演示**必须单 worker**（契约 9）。

```bash
uv sync                       # 或 pip install -e ".[dev]"
cd frontend && npm install
python -m backend.utils.init_db
npm run dev        # 前后端同时起；或 npm run backend (5001) / npm run frontend (3000)
```

---

## 12. 已知缺陷与 dead code

> **不要顺手"修复"。** 要么无人使用、要么已有工单在跟；确要动，先确认属于当前工单范围。

**已知缺陷**
- **Kuzu 图谱无分支隔离**：项目级单文件，现只构建时写一次、全程只读，故无影响；工单06（场景后回写图谱）落地前必须先解决（推理见工单06）。
- `Branch.scenes` 恒为空数组，无写入方；前端用 `GET .../scenes?branch_id=` 查。
- `pause` 走正常终止路径，场景最终是 `completed`，与 `SceneStatus.PAUSED` 无关，前端提示"已中断"。
- 角色回复的 prompt 层输出边界（工单30c）暂缓：无标记的续写仍会进公开对白；主因是 4 位量化服务商，细节见 `llm.md`。
- `DialogueTurn.timestamp` 反序列化时不还原，读出一律是当前时间（同陷阱 16 的 `created_at`）。
- **chromadb 缺失时契约 6 的降级实际不生效**：`memory/embeddings.py` 无条件 `import chromadb`，`long_term` 导入即失败（不要顺手修，待建单；详见 `memory.md`）。
- 后置快照冻结的是内存副本，理论上可能带过时的 `inherited_story_history`；正常路径进不来，难以复现。

**Dead code**
- `knowledge_graph/queries.py`、`agents/base_agent.py` + `CharacterAgent.get_autogen_agent()`、`MemoryManager.snapshot()` / `restore()`（真实路径是 `_collect_states()` + `prime()`）：零调用。
- `DirectorAgent.query_graph()`：零调用且 `GraphManager` 从未 `connect()`；**导演目前不读图谱**。
- `SnapshotManager.restore_snapshot()`：破坏性，**不要接回任何写路径**（陷阱 10；有 `confirm_destructive=True` 路障）。
- `CharacterState.long_term_memory_snapshot`（恒空）、`settings.GRAPHRAG_LLM_MODEL`（从不读取）；`scene_config.py`、`snapshot/models.py` 仅再导出，**不要往里加定义**。

---

## 13. 【设想】

私有内心 OS、MCTS / 多结局等未实现规划登记在 `docs/fix-tickets/README.md`「设想清单」。不要当现状引用；要做时先开工单、先补接口。

---

## 14. 文档维护规则

**落位规则**——写新内容前先问"删掉后会不会有人重新写出这个 bug"：全局影响的契约 / 代码地图 / 已知缺陷 → 本文件（≲280 行）；
改某处代码必须知道的约束（陷阱、细则、"为什么"一句话）→ `.claude/rules/*.md`；推理过程、发现经过、评审时间线、旧实现 →
`docs/fix-tickets/NN-*.md`（规则里放 `详见 工单NN#锚点`）；过时 / 被推翻 / 与代码不符 → **删除或改正**。

- `NOTES.md` 冻结；有效结论升格进规则。**陷阱编号**全局唯一不变，新陷阱从 29 起并补 7.1 索引。
- **规则文件 ≤30KB**，超了就拆；新增规则文件的 `paths` 要覆盖调用方，用 Glob 核实匹配到文件。
- 同步本文件：核心依赖变化 → 第 2 节；目录结构 → 第 3 节；契约 → 第 7 节；修掉第 12 节缺陷 / 落地设想 → 删条目并升格到正文；API 变化 → `api.md`。
- **不要**新建"变更说明.md"，**也不要在任何文档末尾追加变更记录**（看 git log）；在途任务写进 `docs/fix-tickets/`，完成后更新其 `README.md` 索引。
