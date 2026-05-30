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


class GraphRAGError(PlotSystemError):
    pass


class MemoryError(PlotSystemError):
    pass


class LLMError(PlotSystemError):
    pass
