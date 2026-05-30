# CLAUDE.md — PlotSystem 影视多智能体剧情推演系统

> 本文档是项目的**唯一权威规范**，供 AI 编程助手（Claude、Copilot 等）和人类开发者共同遵守。
> 所有架构决策、模块接口、开发约定均以此文档为准。

---

## 目录

1. [项目概述](#1-项目概述)
2. [技术栈总览](#2-技术栈总览)
3. [项目目录结构](#3-项目目录结构)
4. [核心概念与数据模型](#4-核心概念与数据模型)
5. [模块详细规范](#5-模块详细规范)
   - 5.1 [GraphRAG 处理管线](#51-graphrag-处理管线)
   - 5.2 [角色智能体（CharacterAgent）](#52-角色智能体characteragent)
   - 5.3 [导演智能体（DirectorAgent）](#53-导演智能体directoragent)
   - 5.4 [场景引擎（SceneEngine）](#54-场景引擎sceneengine)
   - 5.5 [记忆系统（MemorySystem）](#55-记忆系统memorysystem)
   - 5.6 [快照与分支管理（SnapshotManager）](#56-快照与分支管理snapshotmanager)
   - 5.7 [总结智能体（SummaryAgent）](#57-总结智能体summaryagent)
   - 5.8 [后端 API（FastAPI）](#58-后端-apifastapi)
   - 5.9 [前端（Vue 3 + Vite）](#59-前端vue-3--vite)
6. [API 接口规范](#6-api-接口规范)
7. [开发规范与约定](#7-开发规范与约定)
8. [环境配置](#8-环境配置)
9. [开发优先级路线图](#9-开发优先级路线图)
10. [常见问题与禁止事项](#10-常见问题与禁止事项)

---

## 1. 项目概述

### 1.1 定位

PlotSystem 是一个**影视多智能体、多分枝剧情推演系统**。

核心工作流：
```
种子文本（小说/剧本/世界观文档）
    ↓ GraphRAG 处理
知识图谱 + 角色实体 + 世界规则
    ↓ 角色初始化
多个 CharacterAgent（具有独立记忆、视角、目标）
    ↓ DirectorAgent 规划场景
场景快照 → AutoGen 多轮对话模拟 → 导演决策（回滚/继续/下一场）
    ↓ 分支树管理
多版本场景日志
    ↓ SummaryAgent
网文 / 剧本 / 报告 输出
```

### 1.2 设计原则

- **信息不对称**：每个 CharacterAgent 只持有其"已知信息"，不得访问全局知识图谱
- **快照可回滚**：任何场景模拟前必须创建快照，支持带新初始条件重新模拟
- **导演主权**：DirectorAgent 是唯一的全局协调者，负责所有跨场景决策
- **本地优先**：所有依赖均可本地部署，无强制云服务依赖
- **模块解耦**：各模块通过明确的 Python 接口通信，不直接跨模块访问内部状态

### 1.3 团队信息

- 团队规模：4人，AI 辅助开发为主
- 许可证：MIT（除非另有说明）
- Python 版本：≥ 3.11，≤ 3.12

---

## 2. 技术栈总览

| 层次 | 技术选型 | 版本要求 | 说明 |
|------|----------|----------|------|
| **场景引擎** | AutoGen 0.4 (`autogen-agentchat`) | `>=0.4.0` | GroupChat 模式驱动角色对话 |
| **动态记忆/RAG** | LlamaIndex + ChromaDB | `>=0.10.0` / `>=0.4.0` | 角色短期动态记忆检索 |
| **知识图谱** | Kuzu（嵌入式） | `>=0.6.0` | 支持 Cypher，文件级持久化，无需 Docker |
| **GraphRAG** | microsoft/graphrag | `>=1.0.0` | 种子文本 → 实体关系提取，内置 Kuzu 支持 |
| **快照存储** | SQLite + JSON 文件树 | 内置 | 场景状态快照与分支索引 |
| **后端框架** | FastAPI + Uvicorn | `>=0.110.0` | 异步 API，SSE 推送模拟进度 |
| **前端框架** | Vue 3 + Vite | Vue `>=3.4` | 参考 MiroFish 布局风格 |
| **图谱可视化** | AntV G6 | `>=5.0.0` | 知识图谱渲染，支持大规模节点 |
| **状态管理** | Pinia | `>=2.0.0` | Vue 3 官方推荐 |
| **HTTP 客户端** | Axios | `>=1.6.0` | 前端请求后端 |
| **LLM 接入** | OpenAI SDK（兼容格式） | `>=1.0.0` | 支持任意 OpenAI 格式 API |

---

## 3. 项目目录结构

```
PlotSystem/
├── CLAUDE.md                    # 本文件，项目唯一权威规范
├── README.md                    # 用户面向的简要说明
├── .env.example                 # 环境变量模板
├── .env                         # 实际配置（不入 git）
├── .gitignore
├── pyproject.toml               # Python 项目配置（uv 管理）
├── package.json                 # 根级脚本（同时启动前后端）
│
├── backend/                     # Python 后端
│   ├── main.py                  # FastAPI 入口
│   ├── config.py                # 全局配置加载（从 .env）
│   │
│   ├── graphrag_pipeline/       # GraphRAG 处理管线
│   │   ├── __init__.py
│   │   ├── pipeline.py          # 主管线：种子文本 → 图谱
│   │   ├── entity_extractor.py  # 实体与关系提取
│   │   ├── persona_builder.py   # 从图谱生成角色 Persona
│   │   └── world_rules.py       # 世界规则条目提取
│   │
│   ├── agents/                  # 智能体核心
│   │   ├── __init__.py
│   │   ├── character_agent.py   # CharacterAgent 定义
│   │   ├── director_agent.py    # DirectorAgent 定义
│   │   ├── summary_agent.py     # SummaryAgent 定义
│   │   └── base_agent.py        # 公共基类
│   │
│   ├── scene_engine/            # 场景引擎
│   │   ├── __init__.py
│   │   ├── engine.py            # AutoGen GroupChat 封装
│   │   ├── scene_config.py      # 场景配置数据类
│   │   └── termination.py       # 场景终止条件
│   │
│   ├── memory/                  # 记忆系统
│   │   ├── __init__.py
│   │   ├── memory_manager.py    # 统一记忆管理接口
│   │   ├── long_term.py         # 长期记忆（ChromaDB + LlamaIndex）
│   │   ├── short_term.py        # 短期记忆（对话窗口管理）
│   │   └── episodic.py          # 事件摘要记忆
│   │
│   ├── knowledge_graph/         # 知识图谱层
│   │   ├── __init__.py
│   │   ├── graph_manager.py     # Kuzu 图操作统一接口
│   │   ├── schema.py            # 图谱 Schema 定义
│   │   └── queries.py           # 常用 Cypher 查询集合
│   │
│   ├── snapshot/                # 快照与分支管理
│   │   ├── __init__.py
│   │   ├── snapshot_manager.py  # 快照创建/恢复/删除
│   │   ├── branch_tree.py       # 分支树数据结构
│   │   └── models.py            # 快照数据模型
│   │
│   ├── api/                     # API 路由层
│   │   ├── __init__.py
│   │   ├── projects.py          # 项目管理路由
│   │   ├── characters.py        # 角色管理路由
│   │   ├── scenes.py            # 场景控制路由
│   │   ├── director.py          # 导演决策路由
│   │   └── output.py            # 输出导出路由
│   │
│   └── utils/
│       ├── __init__.py
│       ├── logger.py            # 统一日志
│       └── serializer.py        # 序列化工具
│
├── frontend/                    # Vue 3 前端
│   ├── index.html
│   ├── vite.config.ts
│   ├── package.json
│   ├── tsconfig.json
│   │
│   └── src/
│       ├── main.ts
│       ├── App.vue
│       │
│       ├── pages/
│       │   ├── Workspace.vue    # 主工作台：种子上传 + 图谱可视化
│       │   ├── Director.vue     # 导演视角：场景树 + 分支管理
│       │   └── Output.vue       # 输出：日志 + 文本导出
│       │
│       ├── components/
│       │   ├── GraphViewer.vue  # AntV G6 知识图谱组件
│       │   ├── SceneTree.vue    # 分支树可视化组件
│       │   ├── CharacterCard.vue# 角色卡片组件
│       │   ├── DialogLog.vue    # 对话日志滚动组件
│       │   └── DirectorPanel.vue# 导演决策面板
│       │
│       ├── stores/              # Pinia 状态
│       │   ├── project.ts
│       │   ├── characters.ts
│       │   ├── scenes.ts
│       │   └── director.ts
│       │
│       ├── api/
│       │   └── client.ts        # Axios 封装 + API 函数
│       │
│       └── types/
│           └── index.ts         # 前端 TypeScript 类型定义
│
├── data/                        # 运行时数据（不入 git）
│   ├── projects/                # 每个项目独立目录
│   │   └── {project_id}/
│   │       ├── seed_texts/      # 原始种子文本
│   │       ├── graphrag_output/ # GraphRAG 处理结果
│   │       ├── kuzu_db/         # Kuzu 图数据库文件
│   │       ├── chroma_db/       # ChromaDB 向量数据
│   │       └── snapshots/       # 快照 JSON 文件
│   └── projects.db              # SQLite：项目/场景/分支索引
│
└── tests/
    ├── test_graphrag_pipeline.py
    ├── test_character_agent.py
    ├── test_scene_engine.py
    ├── test_snapshot_manager.py
    └── conftest.py
```

---

## 4. 核心概念与数据模型

### 4.1 Project（项目）

一个推演项目对应一部作品/世界观，包含其所有角色、场景、分支。

```python
@dataclass
class Project:
    project_id: str          # UUID
    name: str                # 项目名称
    description: str         # 简述
    seed_texts: list[str]    # 种子文本文件路径列表
    status: str              # "initializing" | "ready" | "simulating"
    created_at: datetime
    updated_at: datetime
```

### 4.2 CharacterCard（角色卡）

灵感来源：SillyTavern 角色卡设计，扩展了记忆层和信息不对称字段。

```python
@dataclass
class CharacterCard:
    character_id: str
    project_id: str
    name: str
    # --- Persona（SillyTavern 风格）---
    persona: str             # 性格、背景、说话风格描述
    appearance: str          # 外貌描述
    speech_style: str        # 说话习惯、口癖
    # --- 世界观注入 ---
    world_lore_entries: list[LoreEntry]   # 按相关性动态注入的世界观条目
    # --- 信息不对称（关键）---
    known_facts: list[str]   # 该角色"已知"的信息
    unknown_facts: list[str] # 该角色"不知道"的信息（仅导演可见）
    # --- 关系 ---
    relationships: dict[str, RelationshipState]  # {character_id: 关系状态}
    # --- 当前状态 ---
    current_emotion: str
    current_goal: str
    current_location: str
```

### 4.3 LoreEntry（世界观条目）

```python
@dataclass
class LoreEntry:
    lore_id: str
    content: str             # 世界观描述文本
    keywords: list[str]      # 触发关键词
    scope: str               # "global"（所有角色可见）| "character:{id}"（特定角色）
    priority: int            # 注入优先级 1-10
```

### 4.4 Scene（场景）

```python
@dataclass
class Scene:
    scene_id: str
    project_id: str
    branch_id: str           # 所属分支
    parent_scene_id: str | None
    name: str
    description: str         # 导演对场景的描述和目标
    participating_characters: list[str]   # character_id 列表
    location: str
    initial_conditions: dict  # 导演设定的初始条件/变量
    max_turns: int           # 最大模拟轮次
    status: str              # "pending" | "running" | "paused" | "completed"
    snapshot_id_before: str  # 模拟前快照 ID
    snapshot_id_after: str | None
    turns_completed: int
    dialogue_log: list[DialogueTurn]  # 完整对话记录
```

### 4.5 DialogueTurn（对话轮次）

```python
@dataclass
class DialogueTurn:
    turn_id: str
    scene_id: str
    turn_number: int
    character_id: str        # 发言角色
    character_name: str
    # 分类记录（供 SummaryAgent 使用）
    dialogue: str | None     # 对白内容
    action: str | None       # 行动描述（*斜体*格式）
    inner_thought: str | None  # 内心独白（角色自我视角）
    # 元信息
    timestamp: datetime
    memory_context_used: list[str]  # 本轮检索到的记忆摘要（调试用）
```

### 4.6 Snapshot（快照）

```python
@dataclass
class Snapshot:
    snapshot_id: str
    scene_id: str
    branch_id: str
    created_at: datetime
    # 快照内容
    character_states: dict[str, CharacterState]  # {character_id: 状态}
    scene_context: dict      # 场景上下文变量
    graph_checkpoint: str    # Kuzu 图快照文件路径
    chroma_checkpoint: str   # ChromaDB 集合快照路径
```

```python
@dataclass
class CharacterState:
    character_id: str
    current_emotion: str
    current_goal: str
    current_location: str
    relationships: dict[str, RelationshipState]
    long_term_memory_snapshot: str   # ChromaDB 集合序列化路径
    episodic_summary: str            # 重要事件摘要文本
    short_term_buffer: list[str]     # 最近 N 轮对话缓冲
```

### 4.7 Branch（分支）

```python
@dataclass
class Branch:
    branch_id: str
    project_id: str
    parent_branch_id: str | None
    fork_from_snapshot_id: str | None  # 从哪个快照分叉
    fork_conditions: dict    # 新分支的初始条件差异
    name: str                # 人类可读标签（如"林黛玉未死线"）
    scenes: list[str]        # scene_id 有序列表
    created_at: datetime
    director_notes: str      # 导演对此分支的备注
```

---

## 5. 模块详细规范

### 5.1 GraphRAG 处理管线

**位置**：`backend/graphrag_pipeline/`  
**职责**：将非结构化种子文本转化为知识图谱和角色初始状态

#### 主接口

```python
# backend/graphrag_pipeline/pipeline.py

class GraphRAGPipeline:
    def __init__(self, project_id: str, config: GraphRAGConfig): ...

    async def run(self, seed_text_paths: list[str]) -> PipelineResult:
        """
        主管线入口。依次执行：
        1. 文本分块与清洗
        2. 调用 microsoft/graphrag 提取实体关系
        3. 将实体关系写入 Kuzu 图数据库
        4. 为每个角色实体生成初始 CharacterCard
        5. 提取世界规则，生成 LoreEntry 列表
        返回 PipelineResult 包含所有生成的实体 ID
        """
        ...

    async def extract_entities(self, texts: list[str]) -> list[Entity]: ...
    async def build_graph(self, entities: list[Entity], relations: list[Relation]) -> None: ...
    async def generate_personas(self, entity_ids: list[str]) -> list[CharacterCard]: ...
    async def extract_world_rules(self, texts: list[str]) -> list[LoreEntry]: ...
```

#### Kuzu Schema

```cypher
-- 节点类型
CREATE NODE TABLE Character (
    id STRING,
    name STRING,
    persona TEXT,
    PRIMARY KEY (id)
)

CREATE NODE TABLE Location (
    id STRING,
    name STRING,
    description TEXT,
    PRIMARY KEY (id)
)

CREATE NODE TABLE Event (
    id STRING,
    name STRING,
    description TEXT,
    timestamp_in_story STRING,
    PRIMARY KEY (id)
)

CREATE NODE TABLE Concept (
    id STRING,
    name STRING,
    description TEXT,
    PRIMARY KEY (id)
)

-- 关系类型
CREATE REL TABLE KNOWS (FROM Character TO Character, relation_type STRING, strength FLOAT)
CREATE REL TABLE LOCATED_AT (FROM Character TO Location, time_context STRING)
CREATE REL TABLE PARTICIPATED_IN (FROM Character TO Event, role STRING)
CREATE REL TABLE RELATED_TO (FROM Concept TO Concept, relation STRING)
CREATE REL TABLE MENTIONED_IN (FROM Character TO Concept, context STRING)
```

---

### 5.2 角色智能体（CharacterAgent）

**位置**：`backend/agents/character_agent.py`  
**职责**：封装 AutoGen `AssistantAgent`，注入角色 Persona 和动态记忆，维持角色一致性

#### 核心实现规范

```python
# backend/agents/character_agent.py

class CharacterAgent:
    """
    封装 AutoGen AssistantAgent 的角色智能体。
    每个实例对应一个角色，持有独立的记忆管理器。
    """

    def __init__(
        self,
        character_card: CharacterCard,
        memory_manager: MemoryManager,
        llm_config: dict,
    ): ...

    def build_system_prompt(self, scene_context: dict) -> str:
        """
        构建角色的 system prompt，结构如下：
        1. 角色基础 Persona（固定）
        2. 当前状态（情绪/目标/位置）
        3. 动态注入的 LoreEntry（按关键词相关性筛选）
        4. 已知信息列表（known_facts）
        5. 关系状态摘要
        6. 行为格式指令（对白/动作/内心独白分离）

        【禁止】在 system prompt 中注入 unknown_facts
        【禁止】在 system prompt 中泄露其他角色的内心独白
        """
        ...

    async def retrieve_relevant_memory(self, context: str, top_k: int = 5) -> list[str]:
        """
        从长期记忆中检索与当前场景上下文相关的记忆片段。
        使用 LlamaIndex 向量检索。
        """
        ...

    def get_autogen_agent(self) -> AssistantAgent:
        """返回配置好的 AutoGen AssistantAgent 实例"""
        ...

    async def update_state_after_scene(self, scene_log: list[DialogueTurn]) -> None:
        """场景结束后更新角色状态和长期记忆"""
        ...
```

#### System Prompt 模板

```
你是【{name}】。

【角色设定】
{persona}

【说话风格】
{speech_style}

【当前状态】
- 情绪：{current_emotion}
- 目标：{current_goal}
- 位置：{current_location}

【你所了解的世界】
{lore_entries}

【你知道的事实】
{known_facts}

【人际关系】
{relationship_summary}

【行为格式规范】
- 对白直接说出，无需引号
- 动作用 *星号包裹*，如：*走向窗边*
- 内心独白用 [方括号包裹]，如：[他在说谎]
- 每轮回应必须包含至少一种格式
- 保持角色一致性，不得跳出角色视角
- 你只知道你"已知"的信息，不得使用你不该知道的信息
```

---

### 5.3 导演智能体（DirectorAgent）

**位置**：`backend/agents/director_agent.py`  
**职责**：全局剧情规划、场景设计、模拟后决策（回滚/继续/下一场）

#### 核心实现规范

```python
class DirectorAgent:
    """
    导演智能体，持有全局知识图谱访问权限。
    是唯一可以查看所有角色内部状态的实体。
    """

    def __init__(
        self,
        project_id: str,
        graph_manager: GraphManager,
        snapshot_manager: SnapshotManager,
        llm_config: dict,
    ): ...

    async def plan_scene(
        self,
        branch_id: str,
        narrative_goal: str,
        available_characters: list[CharacterCard],
    ) -> SceneConfig:
        """
        根据叙事目标规划下一场景：
        - 选择参与角色（2-6人为宜）
        - 设定场景位置和初始条件
        - 设定最大轮次
        - 给出场景描述和期望走向（不强制结果）
        """
        ...

    async def evaluate_scene(
        self,
        scene: Scene,
        dialogue_log: list[DialogueTurn],
    ) -> SceneEvaluation:
        """
        场景模拟结束后的评估，返回：
        - 梗概摘要
        - 叙事目标达成度评分（0-10）
        - 戏剧张力评分（0-10）
        - 与主线偏离程度评分（0-10）
        - 推荐决策（continue | next_scene | rollback）
        - 回滚时建议的新初始条件
        """
        ...

    async def make_decision(
        self,
        evaluation: SceneEvaluation,
        human_override: DirectorDecision | None = None,
    ) -> DirectorDecision:
        """
        综合评估结果和人类干预，做出最终决策。
        human_override 非空时优先使用人类决策。
        """
        ...

    async def query_character_state(self, character_id: str) -> CharacterState: ...
    async def query_graph(self, cypher: str) -> list[dict]: ...
```

#### DirectorDecision 数据类

```python
@dataclass
class DirectorDecision:
    decision_type: str       # "continue" | "next_scene" | "rollback"
    # continue 时
    extra_turns: int | None  # 继续的额外轮次
    # next_scene 时
    next_scene_config: SceneConfig | None
    # rollback 时
    rollback_to_snapshot_id: str | None
    new_initial_conditions: dict | None   # 回滚时的新起始条件
    rollback_notes: str | None            # 导演对回滚原因的备注
```

#### SceneEvaluation 评分维度

| 维度 | 说明 | 影响决策 |
|------|------|----------|
| `narrative_goal_score` | 场景是否达成预设叙事目标（0-10） | < 4 建议回滚 |
| `dramatic_tension_score` | 戏剧冲突强度（0-10） | < 3 建议继续或回滚 |
| `plot_deviation_score` | 与主线偏离程度（0-10，0=完全一致） | > 7 警告，> 9 强制关注 |
| `character_consistency_score` | 角色行为是否符合设定（0-10） | < 5 建议回滚 |

---

### 5.4 场景引擎（SceneEngine）

**位置**：`backend/scene_engine/`  
**职责**：驱动 AutoGen GroupChat，管理场景生命周期

```python
class SceneEngine:
    def __init__(
        self,
        scene_config: SceneConfig,
        character_agents: list[CharacterAgent],
        snapshot_manager: SnapshotManager,
    ): ...

    async def run(self) -> SceneResult:
        """
        场景执行主流程：
        1. 创建模拟前快照（snapshot_before）
        2. 初始化 AutoGen GroupChat
        3. 注入场景开场描述（由导演提供）
        4. 驱动多轮对话，每轮：
           a. 角色检索相关记忆，更新 system prompt
           b. 角色生成回应
           c. 解析并存储 DialogueTurn（对白/动作/独白分类）
           d. 检查终止条件
        5. 场景结束：创建模拟后快照（snapshot_after）
        6. 返回 SceneResult（含完整日志）
        """
        ...

    def _parse_turn(self, raw_message: str, character_id: str) -> DialogueTurn:
        """
        解析角色原始回应，按格式规范分离：
        - 对白：普通文本段落
        - 动作：*...*
        - 内心独白：[...]
        """
        ...

    async def _check_termination(self, turns: list[DialogueTurn]) -> bool:
        """
        终止条件（满足任一即停止）：
        - 已达 max_turns
        - 导演智能体发出中断信号
        - 所有角色连续3轮无新信息（对话停滞检测）
        """
        ...
```

#### 场景内角色发言顺序

默认采用 AutoGen `RoundRobinGroupChat`，可在 `SceneConfig` 中配置为：
- `round_robin`：轮流发言（默认）
- `selector`：由 LLM 选择最合适的下一个发言者（更自然，成本略高）
- `random`：随机（不推荐，仅测试用）

---

### 5.5 记忆系统（MemorySystem）

**位置**：`backend/memory/`

#### 三层记忆架构

```
┌─────────────────────────────────────────────────┐
│              MemoryManager（统一接口）            │
├─────────────┬──────────────────┬────────────────┤
│  短期记忆    │    长期记忆       │   事件摘要记忆  │
│ ShortTerm   │   LongTerm       │   Episodic     │
│             │                  │                │
│ 对话窗口缓冲 │ ChromaDB向量存储  │ 重要事件文本摘要 │
│ 最近N轮消息  │ LlamaIndex检索   │ 手动/自动触发   │
│ 无需检索    │ top-k 语义相似    │ 存入长期记忆    │
└─────────────┴──────────────────┴────────────────┘
```

```python
class MemoryManager:
    """每个 CharacterAgent 持有一个独立 MemoryManager 实例"""

    def __init__(self, character_id: str, project_id: str): ...

    async def add_experience(self, turn: DialogueTurn) -> None:
        """将新的对话轮次加入短期记忆缓冲"""
        ...

    async def retrieve(self, query: str, top_k: int = 5) -> list[MemoryChunk]:
        """从长期记忆中检索相关片段，返回按相关性排序的结果"""
        ...

    async def consolidate(self, force: bool = False) -> None:
        """
        将短期记忆转存到长期记忆。
        触发条件：
        - 短期缓冲超过阈值（默认 20 轮）
        - 场景结束时强制触发（force=True）
        - 检测到重要事件（由 episodic 模块标记）
        """
        ...

    async def snapshot(self) -> MemorySnapshot:
        """序列化当前记忆状态，用于快照"""
        ...

    async def restore(self, snapshot: MemorySnapshot) -> None:
        """从快照恢复记忆状态"""
        ...
```

#### 重要事件检测

以下情况自动标记为"重要事件"并触发 episodic 记忆存储：
- 角色关系状态发生重大变化（如结盟→背叛）
- 角色明确陈述改变目标
- 场景中发生死亡/重伤等不可逆事件
- 角色获得关键信息（`known_facts` 更新）

---

### 5.6 快照与分支管理（SnapshotManager）

**位置**：`backend/snapshot/`

```python
class SnapshotManager:
    def __init__(self, project_id: str, db_path: str): ...

    async def create_snapshot(
        self,
        scene_id: str,
        branch_id: str,
        character_states: dict[str, CharacterState],
        label: str = "",
    ) -> Snapshot:
        """
        创建完整快照：
        1. 序列化所有参与角色的 CharacterState
        2. 将 Kuzu 数据库文件复制到快照目录
        3. 导出 ChromaDB 集合到快照目录
        4. 在 SQLite 中注册快照元数据
        返回 Snapshot 对象
        """
        ...

    async def restore_snapshot(self, snapshot_id: str) -> dict[str, CharacterState]:
        """
        从快照恢复：
        1. 从 SQLite 查询快照元数据
        2. 恢复 Kuzu 数据库文件
        3. 恢复 ChromaDB 集合
        4. 反序列化角色状态
        返回 {character_id: CharacterState}
        """
        ...

    async def fork_branch(
        self,
        from_snapshot_id: str,
        new_conditions: dict,
        branch_name: str,
        director_notes: str = "",
    ) -> Branch:
        """
        从快照创建新分支：
        1. 调用 restore_snapshot 恢复状态
        2. 应用 new_conditions 修改（初始条件覆盖）
        3. 创建新 Branch 记录
        4. 返回新 Branch 对象
        """
        ...

    async def get_branch_tree(self, project_id: str) -> BranchTree:
        """获取项目完整分支树（用于前端可视化）"""
        ...
```

#### 快照文件结构

```
data/projects/{project_id}/snapshots/{snapshot_id}/
├── meta.json                    # 快照元数据
├── character_states/
│   ├── {character_id}.json      # 角色状态序列化
│   └── ...
├── kuzu_checkpoint/             # Kuzu 数据库文件副本
│   └── ...
└── chroma_collections/
    ├── {character_id}_memory/   # 各角色 ChromaDB 集合导出
    └── ...
```

---

### 5.7 总结智能体（SummaryAgent）

**位置**：`backend/agents/summary_agent.py`

```python
class SummaryAgent:
    def __init__(self, llm_config: dict): ...

    async def generate_synopsis(
        self,
        scenes: list[Scene],
        style: str = "narrative",
    ) -> str:
        """
        根据场景日志生成梗概摘要。
        style: "narrative"（叙事）| "bullet"（要点）| "timeline"（时间线）
        用途：供导演智能体评估场景时参考
        """
        ...

    async def generate_output(
        self,
        scenes: list[Scene],
        output_format: OutputFormat,
        branch_id: str | None = None,
    ) -> str:
        """
        生成最终输出文本。
        output_format 指定目标格式（见 OutputFormat）
        branch_id 为 None 时处理所有分支，否则只处理指定分支
        """
        ...
```

#### OutputFormat 枚举

```python
class OutputFormat(Enum):
    WEB_NOVEL = "web_novel"      # 网络小说风格（流畅叙事，第三人称）
    SCREENPLAY = "screenplay"     # 剧本格式（标准格式：场景行/人物名/对白）
    STAGE_PLAY = "stage_play"    # 舞台剧本格式
    SUMMARY_REPORT = "summary"   # 推演报告（分析向）
    RAW_LOG = "raw"              # 原始对话日志导出（JSON）
```

---

### 5.8 后端 API（FastAPI）

**位置**：`backend/api/`  
**基础 URL**：`http://localhost:5001/api/v1`

#### 通用响应格式

```json
{
  "success": true,
  "data": {},
  "error": null,
  "timestamp": "2026-05-29T12:00:00Z"
}
```

#### 路由总览

详见第 6 节完整 API 规范。

#### SSE 实时推送

场景模拟过程中通过 SSE 推送进度：

```
GET /api/v1/scenes/{scene_id}/stream
```

SSE 事件类型：
- `turn`：新对话轮次（含完整 DialogueTurn）
- `status`：场景状态变更
- `snapshot`：快照创建完成
- `evaluation`：导演评估结果
- `error`：错误信息

---

### 5.9 前端（Vue 3 + Vite）

**位置**：`frontend/src/`

#### 页面职责

| 页面 | 路由 | 主要功能 |
|------|------|----------|
| `Workspace.vue` | `/` | 项目列表、种子文本上传、GraphRAG 进度、知识图谱 G6 可视化 |
| `Director.vue` | `/director/:projectId` | 分支树、场景配置、实时对话日志（SSE）、导演决策面板 |
| `Output.vue` | `/output/:projectId` | 选择分支/格式、调用 SummaryAgent、预览与导出 |

#### 核心组件规范

**`GraphViewer.vue`**
- 使用 AntV G6 渲染知识图谱
- 节点类型对应不同颜色：`Character`=蓝、`Location`=绿、`Event`=橙、`Concept`=灰
- 支持点击节点查看详情
- 支持搜索高亮

**`SceneTree.vue`**
- 使用 AntV G6 的树形布局渲染分支树
- 每个节点代表一个 Scene，颜色区分状态
- 支持点击节点跳转到对应场景详情

**`DirectorPanel.vue`**
- 显示 `SceneEvaluation` 四维评分（雷达图或柱状图）
- 提供三个决策按钮：继续 / 下一场 / 回滚
- 回滚时显示"新初始条件"编辑框
- 支持导演手动覆盖 AI 建议

**`DialogLog.vue`**
- 实时渲染 SSE 推送的 DialogueTurn
- 对白/动作/独白用不同样式区分
- 支持按角色过滤
- 自动滚动到最新消息

#### 样式规范

- 风格参考 MiroFish（暗色主题、卡片式布局）
- 主色：`#1a1a2e`（背景）、`#16213e`（卡片）、`#0f3460`（强调）、`#e94560`（高亮/危险）
- 字体：系统字体栈，中文优先

---

## 6. API 接口规范

### 项目管理

```
POST   /api/v1/projects                    # 创建项目
GET    /api/v1/projects                    # 列出所有项目
GET    /api/v1/projects/{project_id}       # 项目详情
DELETE /api/v1/projects/{project_id}       # 删除项目

POST   /api/v1/projects/{project_id}/seed  # 上传种子文本
POST   /api/v1/projects/{project_id}/build # 触发 GraphRAG 处理
GET    /api/v1/projects/{project_id}/build/status  # 处理进度
```

### 角色管理

```
GET    /api/v1/projects/{project_id}/characters              # 列出角色
GET    /api/v1/projects/{project_id}/characters/{char_id}    # 角色详情（含 CharacterCard）
PATCH  /api/v1/projects/{project_id}/characters/{char_id}    # 更新角色卡（人工编辑）
GET    /api/v1/projects/{project_id}/characters/{char_id}/memory  # 查看角色记忆
```

### 场景控制

```
POST   /api/v1/projects/{project_id}/scenes           # 创建场景（导演规划）
GET    /api/v1/projects/{project_id}/scenes/{scene_id}# 场景详情
POST   /api/v1/scenes/{scene_id}/start                # 开始模拟
POST   /api/v1/scenes/{scene_id}/pause                # 暂停模拟
GET    /api/v1/scenes/{scene_id}/stream               # SSE 实时流
GET    /api/v1/scenes/{scene_id}/log                  # 完整对话日志
```

### 导演决策

```
GET    /api/v1/scenes/{scene_id}/evaluation           # 获取场景评估
POST   /api/v1/scenes/{scene_id}/decision             # 提交导演决策
  Body: {
    "decision_type": "continue" | "next_scene" | "rollback",
    "extra_turns": 10,                          // continue 时
    "next_scene_description": "...",            // next_scene 时
    "rollback_snapshot_id": "...",              // rollback 时
    "new_initial_conditions": {}               // rollback 时
  }
```

### 快照与分支

```
GET    /api/v1/projects/{project_id}/branches         # 分支树
GET    /api/v1/projects/{project_id}/snapshots        # 快照列表
POST   /api/v1/snapshots/{snapshot_id}/fork           # 从快照创建新分支
DELETE /api/v1/snapshots/{snapshot_id}               # 删除快照
```

### 输出导出

```
POST   /api/v1/projects/{project_id}/output           # 生成输出文本
  Body: {
    "format": "web_novel" | "screenplay" | "stage_play" | "summary" | "raw",
    "branch_id": null,          // null=所有分支，否则指定分支
    "scene_ids": []             // 可选，指定场景子集
  }
GET    /api/v1/output/{output_id}                    # 获取生成结果
```

---

## 7. 开发规范与约定

### 7.1 Python 代码规范

```python
# 类型注解：所有公共函数必须有完整类型注解
async def plan_scene(
    self,
    branch_id: str,
    narrative_goal: str,
) -> SceneConfig:  # ✅ 必须标注返回类型

# 数据类：使用 @dataclass 或 Pydantic BaseModel
# 跨 API 边界的数据（请求/响应）：使用 Pydantic
# 内部传递的数据：使用 @dataclass

# 异步：所有 IO 操作（LLM调用、数据库、文件）必须是 async
# 禁止在 async 函数中使用 time.sleep()，使用 asyncio.sleep()

# 错误处理：明确的自定义异常类
class PlotSystemError(Exception): ...
class SnapshotNotFoundError(PlotSystemError): ...
class SceneEngineError(PlotSystemError): ...
```

### 7.2 命名约定

| 对象 | 命名风格 | 示例 |
|------|----------|------|
| Python 类 | PascalCase | `CharacterAgent`, `SceneEngine` |
| Python 函数/变量 | snake_case | `build_system_prompt`, `character_id` |
| Python 常量 | UPPER_SNAKE | `MAX_TURNS_DEFAULT = 20` |
| API 路由参数 | snake_case | `/scenes/{scene_id}` |
| Vue 组件 | PascalCase | `DirectorPanel.vue` |
| Vue 变量/方法 | camelCase | `currentScene`, `handleDecision` |
| Pinia Store | camelCase | `useProjectStore` |
| 数据库字段 | snake_case | `character_id`, `created_at` |

### 7.3 Git 提交规范

使用 Conventional Commits：

```
feat(agents): add CharacterAgent memory retrieval
fix(scene): resolve termination condition bug
docs(api): update scene endpoint documentation
refactor(snapshot): simplify restore logic
test(director): add evaluation scoring tests
chore: update dependencies
```

### 7.4 AI 助手协作规范

当 AI 助手（Claude/Copilot）生成代码时，必须遵守：

1. **不得违反信息不对称原则**：CharacterAgent 的任何代码不得直接访问 `unknown_facts`
2. **快照前置**：任何启动场景的代码，必须在 `engine.run()` 开头调用 `create_snapshot`
3. **接口优先**：新功能先定义接口（函数签名 + docstring），再实现
4. **不新增依赖**：未在技术栈列表中的库，需在 `CLAUDE.md` 更新后才能使用
5. **测试覆盖**：核心模块（agents、snapshot、memory）的新功能必须附带单元测试
6. **中文注释**：面向业务逻辑的注释用中文，面向技术实现的注释用英文均可

### 7.5 LLM 调用规范

```python
# 统一使用 config.py 中的 LLM 配置，不硬编码模型名
LLM_CONFIG = {
    "model": settings.LLM_MODEL_NAME,
    "api_key": settings.LLM_API_KEY,
    "base_url": settings.LLM_BASE_URL,
}

# 所有 LLM 调用必须设置超时
# 所有 LLM 调用必须有重试逻辑（最多3次）
# 角色 Agent 的 temperature 默认 0.8（创意性）
# 导演 Agent 的 temperature 默认 0.3（一致性）
# 总结 Agent 的 temperature 默认 0.7（平衡）
```

---

## 8. 环境配置

### 8.1 `.env.example`

```dotenv
# LLM API 配置（支持任意 OpenAI 兼容格式）
LLM_API_KEY=your_api_key_here
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL_NAME=qwen-plus

# 后端配置
BACKEND_HOST=0.0.0.0
BACKEND_PORT=5001
DEBUG=false

# 数据存储路径
DATA_DIR=./data

# GraphRAG 配置
GRAPHRAG_LLM_MODEL=qwen-plus
GRAPHRAG_EMBEDDING_MODEL=text-embedding-v3

# 场景引擎默认配置
DEFAULT_MAX_TURNS=20
DEFAULT_SPEAKER_MODE=round_robin   # round_robin | selector

# 记忆配置
SHORT_TERM_BUFFER_SIZE=20          # 短期记忆最大轮次
MEMORY_TOP_K=5                     # RAG 检索返回条数

# 日志级别
LOG_LEVEL=INFO
```

### 8.2 依赖安装

```bash
# Python 依赖（推荐使用 uv）
uv sync

# 或使用 pip
pip install -e ".[dev]"

# 前端依赖
cd frontend && npm install
```

### 8.3 启动命令

```bash
# 开发模式（同时启动前后端）
npm run dev

# 单独启动
npm run backend    # FastAPI: http://localhost:5001
npm run frontend   # Vite:    http://localhost:3000

# 初始化数据库
python -m backend.utils.init_db
```

### 8.4 `pyproject.toml` 依赖声明

```toml
[project]
name = "plotsystem"
version = "0.1.0"
requires-python = ">=3.11,<3.13"

dependencies = [
    "autogen-agentchat>=0.4.0",
    "autogen-ext[openai]>=0.4.0",
    "llama-index>=0.10.0",
    "llama-index-vector-stores-chroma>=0.1.0",
    "chromadb>=0.4.0",
    "kuzu>=0.6.0",
    "graphrag>=1.0.0",
    "fastapi>=0.110.0",
    "uvicorn[standard]>=0.27.0",
    "pydantic>=2.5.0",
    "pydantic-settings>=2.0.0",
    "openai>=1.0.0",
    "aiofiles>=23.0.0",
    "aiosqlite>=0.19.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "httpx>=0.27.0",
    "ruff>=0.3.0",
]
```

---

## 9. 开发优先级路线图

### Phase 1：核心骨架（目标：2周内可跑通端到端流程）

```
Week 1:
□ P1.1  项目目录初始化 + pyproject.toml + .env 配置
□ P1.2  Kuzu Schema 建立 + GraphManager 基础接口
□ P1.3  GraphRAG Pipeline 集成（microsoft/graphrag + Kuzu）
□ P1.4  CharacterCard 数据模型 + persona_builder.py
□ P1.5  MemoryManager 基础版（短期缓冲 + ChromaDB存储）

Week 2:
□ P1.6  CharacterAgent 基础版（system prompt构建 + AutoGen集成）
□ P1.7  SceneEngine 基础版（GroupChat + 对话解析 + 日志存储）
□ P1.8  SnapshotManager 基础版（创建 + 恢复）
□ P1.9  DirectorAgent 基础版（场景规划 + 简单评估）
□ P1.10 FastAPI 基础路由（项目CRUD + 场景启动）
□ P1.11 CLI 测试脚本：端到端跑通一个完整场景
```

### Phase 2：分支与前端（目标：再2周，可视化协作）

```
□ P2.1  SnapshotManager 完整版（fork_branch + 分支树）
□ P2.2  DirectorAgent 完整版（evaluate_scene + make_decision）
□ P2.3  SSE 实时推送（场景模拟进度）
□ P2.4  前端 Workspace.vue（项目管理 + G6 图谱）
□ P2.5  前端 Director.vue（分支树 + 实时日志 + 决策面板）
□ P2.6  前端 Output.vue（格式选择 + 预览导出）
□ P2.7  SummaryAgent 完整版（多格式输出）
```

### Phase 3：质量与体验（目标：打磨，可对外展示）

```
□ P3.1  记忆系统完整版（episodic + 重要事件检测 + consolidation）
□ P3.2  世界观 LoreEntry 动态注入优化
□ P3.3  角色关系状态追踪与可视化
□ P3.4  导演评分四维雷达图
□ P3.5  完整测试套件
□ P3.6  性能优化（并发场景、大规模图谱）
□ P3.7  文档完善
```

---

## 10. 常见问题与禁止事项

### ❌ 禁止事项

1. **绝对禁止**：在 CharacterAgent 的 system prompt 或任何角色可见的上下文中注入 `unknown_facts`
2. **禁止**：在未创建快照的情况下启动场景模拟
3. **禁止**：直接在模块间传递 Kuzu/ChromaDB 的内部连接对象，必须通过接口层
4. **禁止**：将 `.env` 中的 API Key 硬编码到任何源文件中
5. **禁止**：在未更新本文档的情况下引入新的核心依赖
6. **禁止**：SummaryAgent 直接访问 CharacterAgent 的 `inner_thought`（只能通过日志中已记录的部分）
7. **禁止**：跳过 Phase 1 的 CLI 验证直接开发前端

### ⚠️ 注意事项

1. GraphRAG 处理消耗 Token 较多，开发阶段建议使用小型种子文本（< 5000字）测试
2. AutoGen GroupChat 的 `selector` 模式每轮额外消耗一次 LLM 调用，调试时使用 `round_robin`
3. Kuzu 不支持真正的事务回滚，快照机制必须在文件层面复制数据库目录
4. ChromaDB 集合导出/导入在大规模数据时较慢，快照频率不宜过高
5. SSE 连接在 Nginx 反向代理时需配置 `proxy_buffering off`

### 📝 本文档更新规范

以下情况**必须**更新 `CLAUDE.md`：
- 新增核心依赖
- 修改模块接口（函数签名、返回类型）
- 修改数据模型字段
- 新增 API 路由
- 修改目录结构
- 修改开发规范

更新时在对应章节末尾添加变更记录：
```
<!-- 变更记录 -->
<!-- 2026-05-29: 初始版本 by Claude -->
```

---

*本文档由 Claude（GitHub Copilot）基于项目设计讨论自动生成，2026-05-29。*
*后续所有对本文档的修改须经团队确认后生效。*

<!-- 变更记录 -->
<!-- 2026-05-29: 初始版本 by Claude -->
<!-- 2026-05-30: 完整实现项目骨架 by Copilot
     - 后端全部模块落地（models 集中、config、utils、knowledge_graph、memory、
       graphrag_pipeline、agents、scene_engine、snapshot、services、api、main）
     - 新增 services 层（orchestrator/repository/events）作为编排与持久化枢纽
     - 可选依赖（kuzu/chromadb/autogen/graphrag）均实现优雅降级，离线可运行
     - 前端 Vue3+Vite+Pinia+G6 全套页面与组件
     - 端到端 CLI 演示 scripts/run_demo.py
     - 测试 14 项全过，ruff 通过（UP042 忽略）
-->

