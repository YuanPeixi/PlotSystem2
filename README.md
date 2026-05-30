# PlotSystem — 影视多智能体剧情推演系统

一个影视多智能体、多分枝剧情推演系统。将种子文本（小说/剧本/世界观）通过 GraphRAG 转化为知识图谱与角色实体，由多个具备独立记忆与信息不对称视角的 CharacterAgent 在场景中互动，由 DirectorAgent 进行全局规划与回滚/分支决策，最终由 SummaryAgent 输出网文/剧本/报告。

> 完整且权威的规范见 [`CLAUDE.md`](./CLAUDE.md)。

## 技术栈

- 场景引擎：AutoGen 0.4
- 记忆/RAG：LlamaIndex + ChromaDB
- 知识图谱：Kuzu（嵌入式）
- GraphRAG：microsoft/graphrag（可选安装）
- 后端：FastAPI + Uvicorn
- 前端：Vue 3 + Vite + Pinia + AntV G6

## 快速开始

### 1. 安装依赖

```bash
# Python（推荐 uv）
uv sync
# 或
pip install -e ".[dev]"

# GraphRAG（可选，依赖较重）
pip install -e ".[graphrag]"

# 前端
cd frontend && npm install
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入 LLM_API_KEY 等
```

### 3. 初始化数据库

```bash
python -m backend.utils.init_db
```

### 4. 启动

```bash
# 同时启动前后端（需先 npm install 安装 concurrently）
npm run dev

# 或分开启动
npm run backend    # http://localhost:5001
npm run frontend   # http://localhost:3000
```

### 5. CLI 端到端验证

```bash
python -m scripts.run_demo
```

## 目录结构

见 `CLAUDE.md` 第 3 节。

## 许可证

MIT
