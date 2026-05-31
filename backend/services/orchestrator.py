"""编排服务：组装 CharacterAgent、运行场景、处理导演决策。

供 API 路由调用，是连接数据持久化与各引擎的核心枢纽。
"""

from __future__ import annotations

from backend.agents import CharacterAgent, DirectorAgent, SummaryAgent
from backend.graphrag_pipeline import GraphRAGPipeline
from backend.knowledge_graph import GraphManager
from backend.memory import MemoryManager
from backend.models import (
    CharacterCard,
    DecisionType,
    DialogueTurn,
    DirectorDecision,
    OutputFormat,
    ProjectStatus,
    Scene,
    SceneConfig,
    SceneEvaluation,
    SceneStatus,
    new_id,
)
from backend.scene_engine import SceneEngine
from backend.services import events, repository
from backend.snapshot import SnapshotManager
from backend.utils.logger import get_logger
from backend.utils.serializer import to_dict

logger = get_logger("orchestrator")

# 运行中的场景引擎注册表（支持暂停/中断）
_running_engines: dict[str, SceneEngine] = {}


# ---------------------------------------------------------------------------
# GraphRAG 构建
# ---------------------------------------------------------------------------

_build_status: dict[str, dict] = {}


def get_build_status(project_id: str) -> dict:
    return _build_status.get(project_id, {"stage": "未开始", "progress": 0.0})


async def run_graphrag(project_id: str) -> None:
    """后台任务：运行 GraphRAG 管线并持久化结果。"""
    project = await repository.get_project(project_id)
    project.status = ProjectStatus.INITIALIZING.value
    await repository.save_project(project)

    async def _progress(stage: str, pct: float) -> None:
        # 保留已有的角色计数等附加字段，仅更新阶段与进度
        prev = _build_status.get(project_id, {})
        _build_status[project_id] = {**prev, "stage": stage, "progress": pct}

    async def _on_character(card: CharacterCard, done: int, total: int) -> None:
        # 角色卡生成后立即持久化，前端轮询即可逐个预览
        await repository.save_character(card)
        prev = _build_status.get(project_id, {})
        _build_status[project_id] = {
            **prev,
            "character_done": done,
            "character_total": total,
        }

    pipeline = GraphRAGPipeline(project_id)
    try:
        result = await pipeline.run(
            project.seed_texts, progress=_progress, on_character=_on_character
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("GraphRAG 处理失败")
        _build_status[project_id] = {"stage": f"失败: {exc}", "progress": 0.0}
        project.status = ProjectStatus.INITIALIZING.value
        await repository.save_project(project)
        return

    # 将 global lore 注入所有角色，character 范围注入对应角色
    global_lore = [e for e in result.lore_entries if e.scope == "global"]
    for card in result.character_cards:
        card.world_lore_entries = list(global_lore) + [
            e for e in result.lore_entries if e.scope.endswith(card.character_id)
        ]
        await repository.save_character(card)

    # 创建主分支
    sm = SnapshotManager(project_id)
    await sm.ensure_main_branch()

    project.status = ProjectStatus.READY.value
    await repository.save_project(project)
    _build_status[project_id] = {
        "stage": "完成",
        "progress": 1.0,
        "entity_count": result.entity_count,
        "relation_count": result.relation_count,
        "character_count": len(result.character_cards),
        "lore_count": len(result.lore_entries),
    }


# ---------------------------------------------------------------------------
# 构建 CharacterAgent
# ---------------------------------------------------------------------------


async def build_character_agents(
    project_id: str, character_ids: list[str]
) -> list[CharacterAgent]:
    agents: list[CharacterAgent] = []
    for cid in character_ids:
        card = await repository.get_character(project_id, cid)
        mem = MemoryManager(cid, project_id)
        await mem.connect()
        agents.append(CharacterAgent(card, mem))
    return agents


# ---------------------------------------------------------------------------
# 导演规划场景
# ---------------------------------------------------------------------------


async def plan_scene(
    project_id: str, branch_id: str, narrative_goal: str
) -> SceneConfig:
    cards = await repository.list_characters(project_id)
    director = DirectorAgent(project_id, GraphManager(project_id), SnapshotManager(project_id))
    return await director.plan_scene(branch_id, narrative_goal, cards)


async def create_scene_from_config(
    project_id: str, branch_id: str, config: SceneConfig
) -> Scene:
    scene = Scene(
        scene_id=new_id(),
        project_id=project_id,
        branch_id=branch_id,
        name=config.name,
        description=config.description,
        participating_characters=config.participating_characters,
        location=config.location,
        initial_conditions=config.initial_conditions,
        max_turns=config.max_turns,
        status=SceneStatus.PENDING.value,
    )
    await repository.save_scene(scene)
    return scene


# ---------------------------------------------------------------------------
# 运行场景（含 SSE 推送）
# ---------------------------------------------------------------------------


async def run_scene(scene_id: str) -> None:
    """后台任务：运行场景并通过事件总线推送进度。"""
    scene = await repository.get_scene(scene_id)
    agents = await build_character_agents(scene.project_id, scene.participating_characters)

    config = SceneConfig(
        name=scene.name,
        description=scene.description,
        participating_characters=scene.participating_characters,
        location=scene.location,
        initial_conditions=scene.initial_conditions,
        max_turns=scene.max_turns,
        opening_narration=scene.initial_conditions.get("opening_narration", ""),
    )
    sm = SnapshotManager(scene.project_id)
    engine = SceneEngine(scene, config, agents, sm)
    _running_engines[scene_id] = engine

    await events.publish(scene_id, "status", {"status": "running"})

    async def _on_turn(turn: DialogueTurn) -> None:
        await events.publish(scene_id, "turn", to_dict(turn))

    try:
        result = await engine.run(on_turn=_on_turn)
        await repository.save_scene(scene)
        await events.publish(scene_id, "snapshot", {"snapshot_id": result.snapshot_id_after})

        # 自动评估
        director = DirectorAgent(
            scene.project_id, GraphManager(scene.project_id), sm
        )
        evaluation = await director.evaluate_scene(scene, result.dialogue_log)
        await repository.save_evaluation(evaluation)
        await events.publish(scene_id, "evaluation", to_dict(evaluation))
        await events.publish(
            scene_id, "status", {"status": "completed", "reason": result.terminated_reason}
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("场景运行失败")
        await events.publish(scene_id, "error", {"message": str(exc)})
    finally:
        _running_engines.pop(scene_id, None)


def pause_scene(scene_id: str) -> bool:
    engine = _running_engines.get(scene_id)
    if engine:
        engine.interrupt()
        return True
    return False


# ---------------------------------------------------------------------------
# 导演决策
# ---------------------------------------------------------------------------


async def apply_decision(
    scene_id: str, human_override: DirectorDecision | None
) -> DirectorDecision:
    scene = await repository.get_scene(scene_id)
    evaluation = await repository.get_evaluation(scene_id)
    if evaluation is None:
        evaluation = SceneEvaluation(scene_id=scene_id)
    director = DirectorAgent(
        scene.project_id, GraphManager(scene.project_id), SnapshotManager(scene.project_id)
    )
    decision = await director.make_decision(evaluation, human_override)

    if decision.decision_type == DecisionType.ROLLBACK.value:
        target = decision.rollback_to_snapshot_id or scene.snapshot_id_before
        if target:
            sm = SnapshotManager(scene.project_id)
            await sm.restore_snapshot(target)
    return decision


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


async def generate_output(
    project_id: str,
    fmt: OutputFormat,
    branch_id: str | None = None,
    scene_ids: list[str] | None = None,
) -> str:
    scenes = await repository.list_scenes(project_id, branch_id)
    if scene_ids:
        scenes = [s for s in scenes if s.scene_id in scene_ids]
    agent = SummaryAgent()
    return await agent.generate_output(scenes, fmt, branch_id)
