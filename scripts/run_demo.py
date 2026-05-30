"""端到端 CLI 验证脚本（对应 P1.11）。

跑通：创建项目 → GraphRAG 构建 → 导演规划 → 场景模拟 → 评估 → 输出。

用法：
    python -m scripts.run_demo

需要在 .env 中配置可用的 LLM_API_KEY。
"""

from __future__ import annotations

import asyncio

from backend.agents import DirectorAgent, SummaryAgent
from backend.config import settings
from backend.knowledge_graph import GraphManager
from backend.models import OutputFormat, Project, ProjectStatus
from backend.services import orchestrator, repository
from backend.snapshot import SnapshotManager
from backend.utils.db import init_db
from backend.utils.logger import get_logger

logger = get_logger("demo")

SEED_TEXT = """\
雨夜，破败的客栈里，剑客萧无名独坐角落，握着一柄无鞘的断剑。
他正在追查三年前灭门惨案的真凶。客栈老板娘柳如烟看似温婉，
实则是江湖闻名的"毒手判官"，她知道萧无名要找的人，正是自己的旧识。
而年轻的捕快沈青此刻也赶到客栈，他奉命缉拿一名通缉要犯——
却不知这要犯与萧无名的仇人是同一人。三人各怀心事，
暴雨封住了去路，一场风暴即将在这小小的客栈中爆发。
世界设定：这是一个武侠世界，江湖中人遵循"快意恩仇"的规则，
官府与江湖势力长期对立，毒术与剑术是两大主流武学体系。
"""


async def main() -> None:
    settings.ensure_dirs()
    await init_db()

    # 1. 创建项目并写入种子文本
    project = Project(name="雨夜客栈推演", description="武侠场景端到端演示")
    await repository.save_project(project)
    seed_dir = settings.project_dir(project.project_id) / "seed_texts"
    seed_dir.mkdir(parents=True, exist_ok=True)
    seed_path = seed_dir / "seed.txt"
    seed_path.write_text(SEED_TEXT, encoding="utf-8")
    project.seed_texts = [str(seed_path)]
    await repository.save_project(project)
    logger.info("✔ 项目已创建：%s", project.project_id)

    # 2. GraphRAG 构建
    logger.info("→ 运行 GraphRAG 构建...")
    await orchestrator.run_graphrag(project.project_id)
    status = orchestrator.get_build_status(project.project_id)
    logger.info("✔ 构建完成：%s", status)

    cards = await repository.list_characters(project.project_id)
    logger.info("✔ 生成角色 %d 个：%s", len(cards), [c.name for c in cards])
    if len(cards) < 2:
        logger.warning("角色不足 2 个，演示终止。请检查 LLM 配置或种子文本。")
        return

    sm = SnapshotManager(project.project_id)
    main_branch = await sm.ensure_main_branch()

    # 3. 导演规划场景
    director = DirectorAgent(project.project_id, GraphManager(project.project_id), sm)
    config = await director.plan_scene(
        main_branch.branch_id,
        "三人在雨夜客栈相遇，气氛逐渐紧张，揭开各自的秘密",
        cards,
    )
    config.max_turns = 6  # 演示用，控制成本
    logger.info("✔ 导演规划场景：%s @ %s", config.name, config.location)

    scene = await orchestrator.create_scene_from_config(
        project.project_id, main_branch.branch_id, config
    )

    # 4. 模拟场景（直接同步运行，不经 SSE）
    logger.info("→ 开始模拟场景...")
    await orchestrator.run_scene(scene.scene_id)
    scene = await repository.get_scene(scene.scene_id)
    logger.info("✔ 场景完成，共 %d 轮对话", scene.turns_completed)
    for t in scene.dialogue_log:
        parts = []
        if t.action:
            parts.append(f"*{t.action}*")
        if t.dialogue:
            parts.append(t.dialogue)
        print(f"  {t.character_name}: {' '.join(parts)}")

    # 5. 评估
    evaluation = await repository.get_evaluation(scene.scene_id)
    if evaluation:
        logger.info(
            "✔ 导演评估：目标=%.1f 张力=%.1f 偏离=%.1f 一致=%.1f → %s",
            evaluation.narrative_goal_score,
            evaluation.dramatic_tension_score,
            evaluation.plot_deviation_score,
            evaluation.character_consistency_score,
            evaluation.recommended_decision,
        )

    # 6. 输出
    logger.info("→ 生成网文输出...")
    summary = SummaryAgent()
    text = await summary.generate_output([scene], OutputFormat.WEB_NOVEL)
    print("\n===== 网文输出 =====\n")
    print(text)

    project.status = ProjectStatus.READY.value
    await repository.save_project(project)
    logger.info("✔ 端到端演示完成。项目 ID：%s", project.project_id)


if __name__ == "__main__":
    asyncio.run(main())
