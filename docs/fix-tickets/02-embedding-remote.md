# 工单02：长期记忆接入远程 Embedding（替代本地降级模型）

**优先级**：P0
**预估改动范围**：小（核心改动集中在 1 个文件 + 1 个配置文件）
**依赖**：无

---

## 1. 背景

项目设计上是"从 Zep（起步套餐 $125/月）降级到本地 ChromaDB 以控制成本"，但当前本地实现**没有正确配置
中文 Embedding**，导致 RAG（角色长期记忆检索）质量远低于预期，且是仓库记忆中记录的
"chromadb→onnxruntime access violation 崩溃"问题的根源。

## 2. 精确错误位置

文件：`backend/memory/long_term.py`

```python
def _connect_sync(self) -> None:
    self.db_dir.mkdir(parents=True, exist_ok=True)
    try:
        self._client = chromadb.PersistentClient(
            path=str(self.db_dir),
            settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
        )
        self._collection = self._client.get_or_create_collection(self.collection_name)
        # ↑ 问题：没有传入 embedding_function 参数！
        # ChromaDB 会静默 fallback 到内置的 onnxruntime 本地小模型 all-MiniLM-L6-v2
        # 该模型面向英文训练，对中文语义检索效果很差，且该 onnxruntime 推理路径
        # 是仓库记忆中记录的"pytest 全量跑会 DLL access violation 崩溃"的根源。
    except Exception as exc:
        ...
```

配套问题：`backend/config.py` 第 54 行定义了

```python
GRAPHRAG_EMBEDDING_MODEL: str = "text-embedding-v3"
```

但全代码搜索（`grep -r "GRAPHRAG_EMBEDDING_MODEL"`）确认这是个**死配置**，从未被任何模块读取使用。

## 3. 目标（Definition of Done）

1. 在 `backend/memory/` 下新增一个 `embeddings.py`（或直接在 `long_term.py` 内新增类），实现一个
   符合 ChromaDB `EmbeddingFunction` 接口的类，内部调用现有 `backend/utils/llm.py` 里已封装的
   OpenAI 兼容客户端（`AsyncOpenAI`，注意 ChromaDB 的 `EmbeddingFunction.__call__` 是**同步**接口，
   需要用 `asyncio.run` 或同步 OpenAI 客户端调用 `client.embeddings.create(...)`，避免在同步上下文里
   直接 await）。
   - 模型名从 `settings.GRAPHRAG_EMBEDDING_MODEL` 读取（让这个配置真正生效）。
   - 需要处理批量输入（ChromaDB 会传入 `list[str]`，一次性调用 embedding API 的 batch 接口）。
   - 需要有失败兜底：调用失败时记录 warning 并可回退到 ChromaDB 默认 embedding（不要让整个系统崩溃）。
2. 修改 `LongTermMemory._connect_sync`，在 `get_or_create_collection` 时传入
   `embedding_function=<新实现的实例>`。
3. `.env.example`（若不存在则在 `backend/config.py` 的 Settings 中确认字段已存在）确保
   `GRAPHRAG_EMBEDDING_MODEL` 可以通过环境变量覆盖，且文档/注释说明"该模型需与 LLM_BASE_URL 指向的
   平台兼容"（如硅基流动同时提供 chat 和 embedding 模型时，可直接复用同一个 `base_url`/`api_key`）。
4. 已存在的 ChromaDB 数据（`data/projects/*/chroma_db/`）因为 embedding 维度/模型变化后**不兼容旧数据**，
   需要在改动说明或 PR 描述中提醒：切换 embedding 后需要清空旧 `chroma_db` 目录重新构建，
   或者做一次性迁移脚本（迁移脚本非必须，简单提醒清空即可）。

## 4. 涉及文件

- `backend/memory/long_term.py`（核心改动）
- `backend/memory/embeddings.py`（新建，embedding function 实现）
- `backend/config.py`（确认 `GRAPHRAG_EMBEDDING_MODEL` 字段，如需要可加 `EMBEDDING_BASE_URL`/`EMBEDDING_API_KEY`
  独立配置项，允许 embedding 模型和 chat 模型使用不同的服务商——建议做成可选，默认复用 `LLM_BASE_URL`/`LLM_API_KEY`）
- `pyproject.toml`（确认 `openai` SDK 版本支持 embeddings 接口，通常已支持，无需改动）

## 5. 实现参考伪代码

```python
# backend/memory/embeddings.py
from openai import OpenAI  # 同步客户端，ChromaDB EmbeddingFunction 要求同步调用
from backend.config import settings
from backend.utils.logger import get_logger

logger = get_logger("memory.embeddings")

class RemoteEmbeddingFunction:
    """符合 chromadb.EmbeddingFunction 协议：__call__(input: list[str]) -> list[list[float]]"""

    def __init__(self):
        self._client = OpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)
        self._model = settings.GRAPHRAG_EMBEDDING_MODEL

    def __call__(self, input: list[str]) -> list[list[float]]:
        try:
            resp = self._client.embeddings.create(model=self._model, input=input)
            return [d.embedding for d in resp.data]
        except Exception as exc:
            logger.warning("远程 embedding 调用失败，回退默认: %s", exc)
            raise  # 或返回占位向量，视 chromadb 版本对异常的容忍度而定
```

在 `LongTermMemory._connect_sync` 中：
```python
from backend.memory.embeddings import RemoteEmbeddingFunction
...
self._collection = self._client.get_or_create_collection(
    self.collection_name,
    embedding_function=RemoteEmbeddingFunction(),
)
```

注意：ChromaDB 不同版本对 `EmbeddingFunction` 的接口协议略有差异（有的要求继承
`chromadb.EmbeddingFunction` 基类并实现 `embed_documents`/`embed_query`，有的是纯 `__call__`），
**实现前务必先看当前项目 `chromadb` 的实际版本**（仓库记忆记录为 `1.5.9`）并查阅该版本文档确认签名。

## 6. 验收方式

1. 清空某测试项目的 `data/projects/{id}/chroma_db/` 目录。
2. 跑一个包含至少 2 轮对话的场景，确认角色记忆检索（`agent.retrieve_relevant_memory`）能返回语义相关
   （而非仅字面重叠）的记忆片段——可以写一个简单脚本调用 `MemoryManager.retrieve("某语义相关但字面不同的查询")`
   验证返回结果合理性。
3. 确认 `pytest` 不再触发 onnxruntime DLL 崩溃（因为不再走本地 ONNX 推理路径）。
4. 确认 `GRAPHRAG_EMBEDDING_MODEL` 修改 `.env` 后，重启服务能生效（不同模型名请求应该发往对应模型）。
