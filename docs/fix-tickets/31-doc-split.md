# 工单31：CLAUDE.md 瘦身与按路径拆分

**优先级**：P2 ｜ **依赖**：当前 `refactor/unified-world-environment` 的改动先提交（CLAUDE.md 也在其中）｜
**契约影响**：不改任何契约的内容，只改它们的存放位置与写法

> 本单只给目标、判据与红线。执行顺序见 §6 的提交拆分，具体怎么压缩由执行者决定。

---

## 1. 现象

`CLAUDE.md` 有 1598 行、144 KB（约 4–5 万 token），**每次会话整份载入**。
官方建议单个文件不超过 200 行：超长不只是费 token，遵从度也会下降。

篇幅分布：§4.2 陷阱 490 行（陷阱 19 有 67 行，16 有 55 行，9、28 各 41 行）、§6 调用链 173 行、
§9 前端 146 行、§7 契约 136 行、§3 代码地图 100 行、§8 API 74 行、§4.1.1 schema 2 65 行。
其中多数内容只在改某一块代码时才用得上，却在每次会话里都占着上下文。

## 2. 为什么必须做

1. **流程决定了它只会变长**：CLAUDE.md §14 与 CONVENTIONS §8 要求"仍然有效的规则升格进正文"，
   每张工单都会往这个常驻文件里加一条陷阱。只删内容不改流程，它会长回来。
2. **四种内容混在一份文件里**：规则、事故经过（日期、"旧实现曾…"）、运维建议（服务商量化过滤）、
   新旧两代实现（schema 1 / 2）。真正需要常驻的只有规则。
3. **已经出现前后矛盾**（见 §7 线索），文件太长，改一处时看不到另一处。
4. **代码注释里按章节号引用的已经开始指错**（见 §7 线索）。章节号一拆就全变，只有陷阱 / 契约编号能当永久 ID。

## 3. 判据（DoD）

### 3.1 体量

- 根 `CLAUDE.md` ≤ 250 行，常驻部分（根文件 + 不带 `paths` 的 rules）≤ 15k 字符。
- 每个 `.claude/rules/*.md` ≤ 15k 字符（硬上限：按需加载的文件在 200K 上下文下超过 40,000 字符会被截断，
  只给开头，后面的规则静默不可见）。

### 3.2 结构

- 细则放 `.claude/rules/*.md`，**每个文件都带 `paths:` frontmatter**（不带 `paths` 的 rules 会在启动时全部载入，等于没拆）。
  按主题分文件，一个文件可以挂多个 glob；建议划分见 §5，执行者可调整。
- 根文件保留：项目定位与工作流、技术栈表、代码地图、**九条契约的精简版**（每条 3–5 行 + 指向细则文件）、
  **陷阱索引**（编号 / 一句话 / 所在文件，一条一行）、数据模型三步 checklist、开发规范要点、
  已知缺陷与 dead code（压缩）、设想清单、文档维护规则。
- 陷阱索引不可省：Grep / Glob **不会**触发 rules 加载，只有 Read / Write / Edit 会。
  只做搜索的会话要靠索引知道"这里有坑、去读哪个文件"。
- 根文件的索引写明每个 rules 文件的路径：其他工具（GPT / Codex）不读 `.claude/rules/`，要能照着索引自己去读。

### 3.3 内容

- **陷阱编号是永久 ID**：不重排、不复用；删掉的留空号，新陷阱从 29 往后编。代码里有近 200 处"陷阱 N""契约 N"引用，
  编号不变它们就一直有效。
- 每条陷阱改成"规则 + 一句理由 + 守护用例 + NOTES 锚点"。事故经过、日期、"旧实现曾…"移到
  `docs/fix-tickets/NOTES.md`（多数已有对应锚点：t26 / t07 / t18 / t12 / t29 / t24-20 / t30 / t30b / director-goal）。
  NOTES 里已有的不重复搬。
- 运维建议（OpenRouter 量化过滤、preset 引用）不属于代码规则，移到 NOTES 或 `.env.example` 注释。
- **schema 1 的处理**（2026-11-07 用户决定）：现有 schema 1 项目都是测试数据，没有保留价值。
  文档不再保留 schema 1 的细则（陷阱 19、20，契约 1 / 3 里 WorldState 进 system 的段落，§6.3.1 I3 的世界变量复制，§6.2 的 world_state 描述）。
  只在根文件或 `world-environment.md` 留一小段：曾有两代世界体系；`world_schema_version` 缺省为 1 的旧路径是兼容层，
  不要再扩展；新功能只做 schema 2。**删兼容层代码不在本单范围**（见 §4 R2）。
- 修掉 §7 列出的矛盾；每一处先对照代码确认哪边对，再改文档。

### 3.4 流程（防止长回来）

- 重写 CLAUDE.md §0.2（文档地图）、§0.3 第 4 条、§14：新规则写进对应的 rules 文件，根文件只加一行索引。
- 同步 CONVENTIONS §8 收尾 checklist、§9 索引维护，以及本目录 README 顶部"想找什么 / 去哪"表。
- 代码注释里按 `CLAUDE.md §x.y` 引用的，改成陷阱 / 契约编号或细则文件名。

## 4. 红线与已排除

- **R1 只搬运、不改语义**：契约与陷阱的判据一个字的意思都不能变。压缩改的是写法，不是规则。
  第一个提交只搬不改，靠脚本证明不丢内容（§5 验收 1）。
- **R2 不动代码逻辑**：本单只改文档、`.gitignore`、代码注释与一个文档检查用例。
  schema 1 兼容层代码要不要删，另开单。
- **R3 不新建零散 md**：新增文件只有 `.claude/rules/*.md` 与本工单。不建"变更说明""迁移记录"。

**已排除**：
- `@import` 拆分：被导入的文件和根文件一起在启动时载入，**不省 token**，只是挪了位置。
- 子目录 `CLAUDE.md`：按目录加载，粒度太粗。`services/` 里编排、持久化、世界三类东西会一起加载。
- `docs/` 放正文、rules 只放 `@import` 转接：rules 里的 import 是否也按需加载，官方文档没写明；
  要兼容 Codex 就得另维护 AGENTS.md。2026-11-07 用户选择正文直接放 `.claude/rules/`。
- 陷阱重新编号：会让代码里近 200 处引用失效。

## 5. 验收

1. **不丢内容**：第一个提交后，用脚本把旧 CLAUDE.md 按非空行切分，核对每一行在"新根文件 + 全部 rules 文件"里恰好出现一次；
   有意删除的（schema 1 段落）列清单附在 PR 正文里。
2. **加载生效**：新会话跑 `/context all`，看常驻部分的体量；打开 `backend/scene_engine/engine.py`，确认终端出现对应 rules 的 `Loaded` 行。
3. **新增 `tests/test_docs_budget.py`**，挡住以后的回潮：
   - 根文件行数 ≤ 250，每个 rules 文件字符数 ≤ 30k（留出到 40k 截断线的余量）；
   - 每个 rules 文件都有可解析的 `paths`，且每个 glob 至少匹配到一个现存文件（防止改名后静默失效）；
   - 代码与测试里引用的每个"陷阱 N"都在根文件索引里。
4. **抽查**：新会话只给一句"continue 续跑时要注意什么"，检查它能经索引找到陷阱 9 并读到 `memory.md`。

## 6. 提交拆分

| 提交 | 内容 |
|---|---|
| 0 | `.gitignore`：`.claude/*` + `!.claude/rules/`（`settings.local.json` 与临时文件不进库） |
| 1 | 只搬不改：原文逐段移到 rules，根文件留索引；附验收 1 的核对结果 |
| 2 | 逐个文件压缩，事故经过移到 NOTES；删 schema 1 细则，留兼容层说明；修 §7 的矛盾 |
| 3 | 流程：CLAUDE.md §0.2 / §0.3 / §14，CONVENTIONS §8 / §9，README；代码注释里的章节号引用 |
| 4 | `tests/test_docs_budget.py` |

## 7. 线索

**建议的 rules 划分**（执行者可调整；`engine.py`、`orchestrator.py` 这类枢纽文件会同时命中多份，压缩质量决定那时省多少）：

| 文件 | paths 大致范围 | 主要内容 |
|---|---|---|
| `scene-engine.md` | `backend/scene_engine/**`、`agents/character_agent.py` | 陷阱 1–4、8、13、24、28，§6.2 |
| `memory.md` | `backend/memory/**`、`services/inspection.py`、`scene_engine/engine.py` | 陷阱 5、9、11、12，§6.5，契约 4 / 6 细节 |
| `world-environment.md` | `services/world*.py`、`services/environment.py`、`services/objects.py`、`agents/environment_agent.py`、`scene_engine/action_intents.py`、世界与物件面板 | §4.1.1，陷阱 25、27，schema 1 兼容层说明 |
| `director-story.md` | `agents/director_agent.py`、`services/storyboard.py`、`services/autopilot.py`、`services/orchestrator.py` | 陷阱 6、7、14–18、21、22，§6.3 |
| `orchestration.md` | `services/orchestrator.py`、`backend/snapshot/**` | 陷阱 10，§5.3，§6.3.1，§6.4，契约 5 / 9 细节 |
| `persistence.md` | `models.py`、`services/repository.py`、`services/world_repository.py`、`utils/db.py`、`utils/fileio.py` | §4.1，§5.1 / 5.2，§5.4 反序列化清单，§10.1 原子写与路径预算 |
| `llm.md` | `utils/llm.py`、`utils/usage.py`、`config.py`、`.env.example`、`memory/embeddings.py`、相关 scripts | §2.1，契约 7 细节，陷阱 26，陷阱 28 的输出上限部分 |
| `build-lore.md` | `backend/graphrag_pipeline/**`、`backend/knowledge_graph/**`、`scripts/reclassify_lore.py` | §6.1，陷阱 23 |
| `api.md` | `backend/api/**`、`backend/main.py`、`frontend/src/api/**`、`frontend/src/stores/**` | §8 与 SSE 事件 |
| `frontend.md` | `frontend/**` | §9 |

**已发现的矛盾**（改之前先对照代码）：
- 陷阱 27 最后一条说"物件状态只在本场有效、跨场延续待 PR-2b"，§4.1.1 与 §13 说 schema 2 已能跨场延续；
- §13 的"环境智能体"一行已标 ✅，按 §14 应移出【设想】、升格到正文；
- §11 写"三路模型"，§2.1 是五路；§3 代码地图里 `config.py` 的注释也还是"三路模型属性"；
- 本目录 README 的"下一步：20 的 PR-2b 写代码"与 24 / 20 行的状态，没反映 schema 2 已取代 PR-2b / PR-3。

**代码注释里已经指错的章节号引用**（举例，不全）：`summary_agent.py` 引"CLAUDE.md 10.6"（不存在）；
`director_agent.py` 引"CLAUDE.md 5.3 评分维度"（5.3 现在是进程内易失状态）；`engine.py` 文件头引"CLAUDE.md 5.4 / 7.4"。
全量用 `CLAUDE\.md *§? *\d|§4\.2` 之类的模式搜。设计单内部的 `§5.x`（指 24-20 设计单）不受影响。

**加载机制的事实**（官方文档 code.claude.com/docs/en/memory，2026-11-07 核对）：
- path-scoped rule 在 Read / Write / Edit 匹配文件时加载（`cat` / `head` 单个文件也算）；Grep / Glob 不触发；
- 按需加载的文件有单文件字符上限（200K 上下文为 40,000 字符），超出只给开头；
- `/compact` 之后根 CLAUDE.md 会重新注入，path-scoped rules 等再次匹配到文件时才重新加载；
- 块级 HTML 注释在注入前会被剥掉（但 §14 已禁止拿它记变更）。
