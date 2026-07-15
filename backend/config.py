"""全局配置加载。从 .env 读取所有运行时配置。

所有模块统一从这里获取配置，禁止硬编码模型名或 API Key。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置。字段名与 .env 中的变量一一对应（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM ---
    LLM_API_KEY: str = "sk-placeholder"
    LLM_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_MODEL_NAME: str = "qwen-plus"
    # 异构模型：未配置时回退到 LLM_MODEL_NAME
    LLM_MODEL_DIRECTOR: str = ""   # 导演：长上下文、指令遵循强
    LLM_MODEL_CHARACTER: str = ""  # 角色：扮演沉浸、创意性强
    LLM_MODEL_SUMMARY: str = ""    # 总结：写作风格好

    @property
    def director_model(self) -> str:
        return self.LLM_MODEL_DIRECTOR or self.LLM_MODEL_NAME

    @property
    def character_model(self) -> str:
        return self.LLM_MODEL_CHARACTER or self.LLM_MODEL_NAME

    @property
    def summary_model(self) -> str:
        return self.LLM_MODEL_SUMMARY or self.LLM_MODEL_NAME

    # --- 后端 ---
    BACKEND_HOST: str = "0.0.0.0"
    BACKEND_PORT: int = 5001
    DEBUG: bool = False

    # --- 数据存储 ---
    DATA_DIR: str = "./data"

    # --- GraphRAG ---
    GRAPHRAG_LLM_MODEL: str = "qwen-plus"
    GRAPHRAG_EMBEDDING_MODEL: str = "text-embedding-v3"
    # 长期记忆 Embedding 的服务商，留空则复用 LLM_API_KEY / LLM_BASE_URL
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_BASE_URL: str = ""

    # --- 场景引擎 ---
    DEFAULT_MAX_TURNS: int = 20
    DEFAULT_SPEAKER_MODE: str = "round_robin"

    # --- 记忆 ---
    SHORT_TERM_BUFFER_SIZE: int = 20
    MEMORY_TOP_K: int = 5

    # --- 日志 ---
    LOG_LEVEL: str = "INFO"

    # ---- 派生属性 ----
    @property
    def data_path(self) -> Path:
        """数据根目录的 Path 对象。"""
        p = Path(self.DATA_DIR).resolve()
        return p

    @property
    def projects_dir(self) -> Path:
        """项目数据目录 data/projects。"""
        return self.data_path / "projects"

    @property
    def projects_db_path(self) -> Path:
        """SQLite 索引数据库路径 data/projects.db。"""
        return self.data_path / "projects.db"

    def project_dir(self, project_id: str) -> Path:
        """指定项目的根目录。"""
        return self.projects_dir / project_id

    def ensure_dirs(self) -> None:
        """确保基础数据目录存在。"""
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    def llm_config(self, temperature: float = 0.7) -> dict:
        """返回统一的 LLM 配置字典（OpenAI 兼容格式）。"""
        return {
            "model": self.LLM_MODEL_NAME,
            "api_key": self.LLM_API_KEY,
            "base_url": self.LLM_BASE_URL,
            "temperature": temperature,
        }


@lru_cache
def get_settings() -> Settings:
    """获取全局单例配置。"""
    settings = Settings()
    settings.ensure_dirs()
    return settings


# 便捷导出
settings = get_settings()
