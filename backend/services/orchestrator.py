"""编排服务：组装 CharacterAgent、运行场景、处理导演决策。

供 API 路由调用，是连接数据持久化与各引擎的核心枢纽。
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.agents import CharacterAgent, DirectorAgent, SummaryAgent
from backend.config import settings
from backend.graphrag_pipeline import GraphRAGPipeline
from backend.knowledge_graph import GraphManager
from backend.memory import MemoryManager
from backend.models import (
    CharacterCard,
    CharacterState,
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

# 正在运行的场景 id 集合，用于防止重复点击"开始模拟"导致同一场景被并发启动多次
# （两个 SceneEngine 并发跑会产生交错/重复的对话轮次，并互相覆盖角色状态持久化结果）。
# 注意：检查与写入必须在同一段没有 await 的同步代码里完成，依赖单线程事件循环保证原子性。
_active_scenes: set[str] = set()


def is_scene_active(scene_id: str) -> bool:
    """查询场景是否已在运行中（供 API 层做前置检查，给出更及时的响应）。"""
    return scene_id in _active_scenes


# ---------------------------------------------------------------------------
# GraphRAG 构建
# ---------------------------------------------------------------------------

_build_status: dict[str, dict] = {}


def _build_status_path(project_id: str) -> Path:
    return settings.project_dir(project_id) / "build_status.json"


def _persist_build_status(project_id: str, status: dict) -> None:
    """将构建进度同步落盘，防止后端重启/前端刷新后进度丢失。"""
    try:
        path = _build_status_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        logger.warning("持久化构建进度失败", exc_info=True)


def _set_build_status(project_id: str, status: dict) -> None:
    _build_status[project_id] = status
    _persist_build_status(project_id, status)


def get_build_status(project_id: str) -> dict:
    status = _build_status.get(project_id)
    if status:
        return status
    # 内存丢失（如后端重启）时从磁盘恢复上次已知进度
    path = _build_status_path(project_id)
    if path.exists():
        try:
            status = json.loads(path.read_text(encoding="utf-8"))
            _build_status[project_id] = status
            return status
        except Exception:  # noqa: BLE001
            logger.warning("读取持久化构建进度失败", exc_info=True)
    return {"stage": "未开始", "progress": 0.0}


async def reconcile_stale_builds() -> None:
    """服务启动时对账：清理上次异常退出遗留的"进行中"构建状态。

    构建进度会持久化到 build_status.json，若后端进程在构建过程中
    异常退出/重启，磁盘上会残留一个进度介于 0~1 之间、既非完成也非
    失败的状态。前端刷新后会误判为"仍在构建"并无限轮询卡死。
    这里在服务启动时扫描所有项目，将这类陈旧状态标记为失败，
    提示用户重新点击构建。
    """
    projects_dir = settings.projects_dir
    if not projects_dir.exists():
        return
    for pdir in projects_dir.iterdir():
        if not pdir.is_dir():
            continue
        status_path = pdir / "build_status.json"
        if not status_path.exists():
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        progress = status.get("progress", 0.0)
        stage = str(status.get("stage", ""))
        if 0 < progress < 1 and not stage.startswith("失败") and not stage.startswith("完成"):
            project_id = pdir.name
            logger.warning(
                "项目 %s 存在未完成的构建状态（可能因服务重启中断），已标记为失败", project_id
            )
            _set_build_status(
                project_id,
                {**status, "stage": "失败: 服务重启导致构建中断，请重新点击构建"},
            )


async def run_graphrag(project_id: str) -> None:
    """后台任务：运行 GraphRAG 管线并持久化结果。"""
    project = await repository.get_project(project_id)
    project.status = ProjectStatus.INITIALIZING.value
    await repository.save_project(project)

    async def _progress(stage: str, pct: float) -> None:
        # 保留已有的角色计数等附加字段，仅更新阶段与进度
        prev = _build_status.get(project_id, {})
        _set_build_status(project_id, {**prev, "stage": stage, "progress": pct})

    async def _on_character(card: CharacterCard, done: int, total: int) -> None:
        # 角色卡生成后立即持久化，前端轮询即可逐个预览
        await repository.save_character(card)
        prev = _build_status.get(project_id, {})
        _set_build_status(
            project_id,
            {
                **prev,
                "character_done": done,
                "character_total": total,
            },
        )

    pipeline = GraphRAGPipeline(project_id)
    try:
        result = await pipeline.run(
            project.seed_texts, progress=_progress, on_character=_on_character
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("GraphRAG 处理失败")
        _set_build_status(project_id, {"stage": f"失败: {exc}", "progress": 0.0})
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
    _set_build_status(
        project_id,
        {
            "stage": "完成",
            "progress": 1.0,
            "entity_count": result.entity_count,
            "relation_count": result.relation_count,
            "character_count": len(result.character_cards),
            "lore_count": len(result.lore_entries),
        },
    )


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
    history = await repository.list_scenes(project_id, branch_id)
    # 只传已完成的场景作为历史上下文
    completed = [s for s in history if s.status == SceneStatus.COMPLETED.value]
    director = DirectorAgent(project_id, GraphManager(project_id), SnapshotManager(project_id))
    return await director.plan_scene(branch_id, narrative_goal, cards, history_scenes=completed)


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
    """后台任务：运行场景并通过事件总线推送进度。

    若场景 dialogue_log 非空（continue 决策续跑），
    会将历史轮次重新注入引擎的起始 transcript，保证角色上下文连贯。
    """
    # 并发/重复启动守卫：检查与写入之间没有 await，避免同一场景被两个后台任务同时跑
    if scene_id in _active_scenes:
        logger.warning("场景 %s 已在运行中，忽略重复启动请求", scene_id)
        return
    _active_scenes.add(scene_id)

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
    # continue 续跑：注入历史 transcript，让角色知道之前说了什么
    if scene.dialogue_log:
        engine.inject_history(scene.dialogue_log)
    _running_engines[scene_id] = engine

    await events.publish(scene_id, "status", {"status": "running"})

    async def _on_turn(turn: DialogueTurn) -> None:
        await events.publish(scene_id, "turn", to_dict(turn))

    try:
        result = await engine.run(on_turn=_on_turn)
        # 持久化角色状态变更（情绪/目标/位置）
        await _persist_character_states(agents)
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
        _active_scenes.discard(scene_id)


async def _persist_character_states(agents: list[CharacterAgent]) -> None:
    """将场景结束后角色的状态（情绪/目标/位置）持久化回角色卡 JSON。"""
    for agent in agents:
        try:
            card = await repository.get_character(agent.card.project_id, agent.character_id)
            # 同步运行时状态到持久化卡片
            card.current_emotion = agent.card.current_emotion
            card.current_goal = agent.card.current_goal
            card.current_location = agent.card.current_location
            card.relationships = agent.card.relationships
            await repository.save_character(card)
        except Exception:  # noqa: BLE001
            logger.warning("持久化角色状态失败：%s", agent.character_id)


async def _apply_character_states(
    project_id: str, states: dict[str, CharacterState]
) -> None:
    """将快照恢复得到的角色状态写回角色卡 JSON（回滚场景使用）。"""
    for cid, state in states.items():
        try:
            card = await repository.get_character(project_id, cid)
            card.current_emotion = state.current_emotion
            card.current_goal = state.current_goal
            card.current_location = state.current_location
            card.relationships = state.relationships
            await repository.save_character(card)
        except Exception:  # noqa: BLE001
            logger.warning("回滚写回角色状态失败：%s", cid)


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
        # 回滚：恢复到模拟前快照，并创建一个新场景重演
        target = decision.rollback_to_snapshot_id or scene.snapshot_id_before
        if target:
            sm = SnapshotManager(scene.project_id)
            restored_states = await sm.restore_snapshot(target)
            # 将恢复的角色状态写回角色卡，保证 API/前端读到的与图谱/记忆一致
            await _apply_character_states(scene.project_id, restored_states)

            new_conditions = decision.new_initial_conditions or scene.initial_conditions
            new_scene = Scene(
                scene_id=new_id(),
                project_id=scene.project_id,
                branch_id=scene.branch_id,
                parent_scene_id=scene.scene_id,
                name=f"{scene.name}（回滚重演）",
                description=scene.description,
                participating_characters=list(scene.participating_characters),
                location=scene.location,
                initial_conditions=new_conditions,
                max_turns=scene.max_turns,
                status=SceneStatus.PENDING.value,
                snapshot_id_before=target,
            )
            await repository.save_scene(new_scene)
            decision.next_scene_id = new_scene.scene_id
            logger.info(
                "回滚场景已创建：%s（%s），恢复自快照 %s",
                new_scene.scene_id,
                new_scene.name,
                target,
            )
        else:
            logger.warning("回滚决策缺少可用快照 ID，场景 %s 未执行回滚", scene_id)

    elif decision.decision_type == DecisionType.CONTINUE.value:
        # 继续：在原场景基础上增加轮次并重新模拟
        extra = decision.extra_turns or 6
        scene.max_turns = scene.turns_completed + extra
        scene.status = SceneStatus.PENDING.value
        await repository.save_scene(scene)
        # 异步触发，调用方通过事件总线追踪进度
        import asyncio
        asyncio.create_task(run_scene(scene_id))
        decision.next_scene_id = scene_id

    elif decision.decision_type == DecisionType.NEXT_SCENE.value:
        # 下一场：让导演根据历史自动规划新场景并创建
        next_desc = getattr(human_override, "next_scene_description", None) if human_override else None
        goal = next_desc or f"延续上一场（{scene.name}）的剧情走向"
        config = await plan_scene(scene.project_id, scene.branch_id, goal)
        new_scene = await create_scene_from_config(scene.project_id, scene.branch_id, config)
        # 记录父子关系
        new_scene.parent_scene_id = scene.scene_id
        await repository.save_scene(new_scene)
        decision.next_scene_id = new_scene.scene_id
        logger.info("下一场场景已创建：%s（%s）", new_scene.scene_id, new_scene.name)

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
