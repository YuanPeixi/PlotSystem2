"""工单24 PR-1a：构建期物件抽取、可见性判定与迁移脚本。

《玻璃王冠》缩写：王冠的外观人人可见，暗格只有诺安知道，暗格的开法只能落在隐藏规则里。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from backend.graphrag_pipeline.pipeline import PipelineResult, build_objects
from backend.graphrag_pipeline.world_rules import (
    LoreVisibilityClassifier,
    ObjectExtractor,
)
from backend.models import CharacterCard, Project, WorldObject
from backend.services import orchestrator, repository
from scripts import extract_objects

_LLM = "backend.graphrag_pipeline.world_rules.chat_safe"

_OPEN_METHOD = "按诺安给的顺序依次按下三块地砖才会打开"
_PROJECTION = "戴上后读取佩戴者最强烈的记忆并投射给在场所有人"

_OBJECTS_REPLY = [
    {
        "name": "玻璃王冠",
        "aliases": ["王冠", "冠", "伊莎贝尔"],
        "public_description": "一顶通体透明的玻璃王冠，放在祭坛的丝绒垫上。",
        "hidden_rules": [_PROJECTION, "仅仅触碰不会触发"],
    },
    {
        "name": "暗格",
        "aliases": ["祭坛暗格"],
        "public_description": "祭坛下方的地砖。",
        "hidden_rules": [_OPEN_METHOD],
    },
    # 模型把人当成了物件
    {"name": "诺安", "aliases": [], "public_description": "宫廷工匠", "hidden_rules": []},
]

_VISIBILITY_REPLY = [
    {"index": 0, "visibility": "public", "known_by": []},
    {"index": 1, "visibility": "private", "known_by": ["诺安"]},
]


def _cards(project_id: str = "p-obj-build") -> list[CharacterCard]:
    return [
        CharacterCard(character_id="c-isa", project_id=project_id, name="伊莎贝尔",
                      known_facts=["自己是唯一继承人"], unknown_facts=["王冠读取记忆"]),
        CharacterCard(character_id="c-noan", project_id=project_id, name="诺安",
                      known_facts=["祭坛下有暗格"], unknown_facts=[]),
        CharacterCard(character_id="c-sev", project_id=project_id, name="塞芙拉",
                      known_facts=[], unknown_facts=["祭坛下有暗格"]),
    ]


class _Router:
    """按 prompt 分辨是抽取还是可见性判定，并记下每次收到的 prompt。"""

    def __init__(self, objects=_OBJECTS_REPLY, visibility=_VISIBILITY_REPLY, fail=()):
        self.objects = objects
        self.visibility = visibility
        self.fail = set(fail)
        self.prompts: list[str] = []

    async def __call__(self, messages, **_kwargs):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        kind = "objects" if "道具设定专家" in prompt else "visibility"
        if kind in self.fail:
            raise RuntimeError("网络抖动")
        return json.dumps(self.objects if kind == "objects" else self.visibility, ensure_ascii=False)


async def _build(router: _Router, project_id: str = "p-obj-build"):
    with patch(_LLM, new=router):
        return await build_objects(
            project_id, ["种子"], _cards(project_id), "种子", ObjectExtractor(), LoreVisibilityClassifier()
        )


@pytest.mark.asyncio
async def test_glass_crown_objects_are_extracted_with_rules_kept_hidden():
    router = _Router()
    extraction, _ = await _build(router)
    by_name = {o.name: o for o in extraction.objects}

    assert set(by_name) == {"玻璃王冠", "暗格"}, "与角色同名的物件必须丢弃"
    crown, niche = by_name["玻璃王冠"], by_name["暗格"]
    assert crown.visibility == "global"
    assert niche.visibility == "character:c-noan"
    assert crown.aliases == ["王冠"], "单字别名与角色名别名必须丢弃"
    assert niche.hidden_rules == [_OPEN_METHOD]
    assert _OPEN_METHOD not in niche.public_description
    assert crown.project_id == "p-obj-build"


@pytest.mark.asyncio
async def test_hidden_rules_never_reach_the_visibility_prompt():
    """分类只看名称与公开描述：隐藏规则按定义谁都不发，送进去只会让模型据此把物件判成 hidden。"""
    router = _Router()
    await _build(router)
    visibility_prompts = [p for p in router.prompts if "剧情设定审校" in p]
    assert visibility_prompts
    for prompt in visibility_prompts:
        assert _OPEN_METHOD not in prompt and _PROJECTION not in prompt


@pytest.mark.asyncio
async def test_classifier_failure_tightens_every_object_to_hidden():
    extraction, _ = await _build(_Router(fail={"visibility"}))
    assert {o.visibility for o in extraction.objects} == {"hidden"}


@pytest.mark.asyncio
async def test_multi_holder_private_object_is_tightened_to_hidden():
    reply = [
        {"index": 0, "visibility": "private", "known_by": ["诺安", "伊莎贝尔"]},
        {"index": 1, "visibility": "hidden", "known_by": []},
    ]
    extraction, _ = await _build(_Router(visibility=reply))
    assert {o.visibility for o in extraction.objects} == {"hidden"}


@pytest.mark.asyncio
async def test_duplicate_mentions_across_chunks_are_merged():
    router = _Router(objects=[
        {"name": "玻璃王冠", "aliases": ["王冠"], "public_description": "透明。", "hidden_rules": ["甲"]},
        {"name": "玻璃王冠", "aliases": ["水晶冠"], "public_description": "别的描述", "hidden_rules": ["乙", "甲"]},
    ], visibility=[{"index": 0, "visibility": "public", "known_by": []}])
    extraction, _ = await _build(router)
    [crown] = extraction.objects
    assert crown.aliases == ["王冠", "水晶冠"]
    assert crown.hidden_rules == ["甲", "乙"]
    assert crown.public_description == "透明。"


@pytest.mark.asyncio
async def test_build_saves_objects_without_overwriting_existing_ones():
    pid = "p-obj-build-run"
    await repository.save_project(Project(project_id=pid, name="玻璃王冠"))
    # 用户上次手改过的王冠：重新构建不能覆盖
    await repository.save_object(WorldObject(
        object_id="o-user", project_id=pid, name="王冠", public_description="用户写的", visibility="global"
    ))
    extraction, verdicts = await _build(_Router(), pid)
    result = PipelineResult(
        character_cards=_cards(pid), objects=extraction.objects, object_verdicts=verdicts
    )

    class _FakePipeline:
        def __init__(self, _project_id):
            pass

        async def run(self, *_args, **_kwargs):
            return result

    with patch.object(orchestrator, "GraphRAGPipeline", _FakePipeline):
        await orchestrator.run_graphrag(pid)

    objects = {o.name: o for o in await repository.list_objects(pid)}
    assert set(objects) == {"王冠", "暗格"}, "别名撞上已有物件名的「玻璃王冠」应跳过"
    assert objects["王冠"].public_description == "用户写的"
    status = orchestrator._build_status[pid]
    assert status["object_count"] == 1 and status["object_hidden"] == 0


@pytest.mark.asyncio
async def test_pipeline_survives_object_extraction_crash():
    """物件是附加层：抽取整个崩掉也不让构建失败。"""
    from backend.graphrag_pipeline.pipeline import GraphRAGPipeline

    pipeline = GraphRAGPipeline("p-obj-crash")
    pipeline.extractor.extract_many = AsyncMock(return_value=([], []))
    pipeline.build_graph = AsyncMock()
    pipeline.persona_builder.build_many = AsyncMock(return_value=[])
    pipeline.world_rules.extract = AsyncMock(return_value=[])
    pipeline.objects.extract = AsyncMock(side_effect=RuntimeError("boom"))
    result = await pipeline.run([])
    assert result.objects == []


# ---------------------------------------------------------------------------
# 迁移脚本
# ---------------------------------------------------------------------------


async def _seeded_project(pid: str, tmp_data_dir) -> None:
    seed = tmp_data_dir / f"{pid}-seed.txt"
    seed.write_text("玻璃王冠与祭坛暗格的故事", encoding="utf-8")
    await repository.save_project(Project(project_id=pid, name="旧项目", seed_texts=[str(seed)]))
    for card in _cards(pid):
        await repository.save_character(card)


@pytest.mark.asyncio
async def test_script_preview_does_not_write(tmp_data_dir):
    pid = "p-obj-script-preview"
    await _seeded_project(pid, tmp_data_dir)
    with patch(_LLM, new=_Router()):
        report = await extract_objects.extract_project_objects(pid)
    assert {o.name for o in report.fresh} == {"玻璃王冠", "暗格"}
    assert await repository.list_objects(pid) == []


@pytest.mark.asyncio
async def test_script_apply_writes_only_new_objects(tmp_data_dir):
    pid = "p-obj-script-apply"
    await _seeded_project(pid, tmp_data_dir)
    await repository.save_object(WorldObject(object_id="o-niche", project_id=pid, name="暗格", visibility="hidden"))
    with patch(_LLM, new=_Router()):
        report = await extract_objects.extract_project_objects(pid, apply=True)
    assert not report.aborted and report.skipped == ["暗格"]
    names = sorted(o.name for o in await repository.list_objects(pid))
    assert names == ["暗格", "玻璃王冠"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", ["objects", "visibility"])
async def test_script_apply_aborts_on_any_call_failure(tmp_data_dir, fail):
    """有人在场就不收紧着写：写一半的物件表比不写更难收拾。"""
    pid = f"p-obj-script-fail-{fail}"
    await _seeded_project(pid, tmp_data_dir)
    with patch(_LLM, new=_Router(fail={fail})):
        report = await extract_objects.extract_project_objects(pid, apply=True)
    assert report.aborted and report.failures
    assert await repository.list_objects(pid) == []
