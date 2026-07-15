"""远程 Embedding 实现，替代 ChromaDB 默认的本地 onnxruntime 小模型。

背景（docs/fix-tickets/02-embedding-remote.md）：
ChromaDB 在未显式传入 embedding_function 时，会静默 fallback 到内置的
onnxruntime 本地模型 all-MiniLM-L6-v2。该模型面向英文训练，中文检索效果差，
且部分 Windows 环境下 onnxruntime 的 DLL 加载会失败（缺 VC++ 运行库等），
导致"ChromaDB 写入/检索失败"的连锁问题。

这里改为调用项目已配置的 OpenAI 兼容 Embedding 接口（模型名取
settings.GRAPHRAG_EMBEDDING_MODEL），彻底绕开本地 onnxruntime 推理路径。
"""

from __future__ import annotations

from typing import Any

from chromadb.api.types import Documents, Embeddings, EmbeddingFunction
from openai import OpenAI

from backend.config import settings
from backend.utils.logger import get_logger

logger = get_logger("memory.embeddings")


class RemoteEmbeddingFunction(EmbeddingFunction[Documents]):
    """符合 ChromaDB EmbeddingFunction 协议的远程 Embedding。

    继承官方基类以获得 embed_query 等默认实现（检索时 ChromaDB 会调用 embed_query，
    仅实现 __call__ 会导致 AttributeError）。
    使用同步 OpenAI 客户端（ChromaDB 要求 __call__ 同步），
    调用方（LongTermMemory）通过 asyncio.to_thread 在线程中执行，避免阻塞事件循环。
    """

    def __init__(self, model: str | None = None):
        self._model = model or settings.GRAPHRAG_EMBEDDING_MODEL
        self._client = OpenAI(
            api_key=settings.EMBEDDING_API_KEY or settings.LLM_API_KEY,
            base_url=settings.EMBEDDING_BASE_URL or settings.LLM_BASE_URL,
        )

    def __call__(self, input: Documents) -> Embeddings:
        resp = self._client.embeddings.create(model=self._model, input=list(input))
        return [d.embedding for d in resp.data]

    @staticmethod
    def name() -> str:
        return "remote_openai_compatible"

    def get_config(self) -> dict[str, Any]:
        return {"model": self._model}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> "RemoteEmbeddingFunction":
        return RemoteEmbeddingFunction(model=config.get("model"))
