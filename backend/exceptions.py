"""自定义异常类。"""

from __future__ import annotations


class PlotSystemError(Exception):
    """所有业务异常的基类。"""


class ProjectNotFoundError(PlotSystemError):
    pass


class CharacterNotFoundError(PlotSystemError):
    pass


class SceneNotFoundError(PlotSystemError):
    pass


class SnapshotNotFoundError(PlotSystemError):
    pass


class BranchNotFoundError(PlotSystemError):
    pass


class SceneEngineError(PlotSystemError):
    pass


class ConflictError(PlotSystemError):
    """请求与当前系统状态冲突（如同一场景的决策正在处理中，重复提交）。"""

    pass


class InvalidRequestError(PlotSystemError):
    """请求内容不合法（如分镜稿超出预算、引用了不存在的节拍）→ 422。

    Pydantic 只校验得了形状；预算这类要按 token 估算的约束只能在业务层判断，
    又不能静默截断用户写的内容，所以需要一个显式的 422。
    """

    pass


class GraphRAGError(PlotSystemError):
    pass


class MemoryError(PlotSystemError):
    pass


class LLMError(PlotSystemError):
    pass
