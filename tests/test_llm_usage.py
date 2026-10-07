"""工单25：场景级 LLM 调用与 token 计数。

计数点只在 `utils/llm.py`（以及契约7 唯一的例外 `memory/embeddings.py`），归属靠 ContextVar。
这里钉住：计数口径、归属不串、评估与场景分开、两条红线（推送评估后不再 save_scene；
run_scene 自己装计数器），以及"漏标用途"在静态检查里就被拦下。
"""

from __future__ import annotations

import ast
import asyncio
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from tenacity import wait_none

from backend.exceptions import LLMError
from backend.models import (
    DialogueTurn,
    LLMPurpose,
    LLMUsageStat,
    Project,
    Scene,
    SceneEvaluation,
    SceneResult,
    WorldObject,
)
from backend.services import events, orchestrator, repository
from backend.utils import llm
from backend.utils.usage import (
    UsageMeter,
    activate,
    current_meter,
    deactivate,
    deserialize_usage,
    total,
    usage_scope,
)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 假客户端：按脚本依次返回响应或抛异常
# ---------------------------------------------------------------------------


def _resp(text: str = "好", usage: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=usage
    )


def _usage(prompt: object, completion: object) -> SimpleNamespace:
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)


@pytest.fixture
def fake_llm(monkeypatch):
    """`script` 是队列：元素为响应或异常；空了就返回默认响应。返回实际收到的请求列表。"""
    script: list = []
    requests: list[dict] = []

    async def create(**kwargs):
        requests.append(kwargs)
        await asyncio.sleep(0)  # 让出调度，制造并发交错
        item = script.pop(0) if script else _resp(usage=_usage(10, 2))
        if isinstance(item, Exception):
            raise item
        return item

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_client", lambda base_url=None, api_key=None: client)
    # 重试不真的等
    monkeypatch.setattr(llm._complete.retry, "wait", wait_none())
    return SimpleNamespace(script=script, requests=requests)


async def _call(purpose=LLMPurpose.CHARACTER, text: str = "你好") -> str:
    return await llm.chat_safe([{"role": "user", "content": text}], purpose=purpose)


# ---------------------------------------------------------------------------
# llm.py 的计数口径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_call_takes_tokens_from_provider_usage(fake_llm):
    fake_llm.script.append(_resp(usage=_usage(123, 45)))
    with usage_scope() as meter:
        assert await _call() == "好"
    stat = meter.snapshot()["character"]
    assert (stat.calls, stat.prompt_tokens, stat.completion_tokens) == (1, 123, 45)
    assert stat.estimated_calls == 0 and stat.failures == 0 and stat.retries == 0
    assert stat.seconds >= 0


@pytest.mark.asyncio
async def test_missing_usage_is_estimated_and_marked(fake_llm):
    fake_llm.script.append(_resp(text="回答五个字", usage=None))
    with usage_scope() as meter:
        await _call(text="一二三四五六七八")
    stat = meter.snapshot()["character"]
    assert stat.calls == 1 and stat.estimated_calls == 1
    assert stat.prompt_tokens == llm.estimate_tokens("一二三四五六七八")
    assert stat.completion_tokens == llm.estimate_tokens("回答五个字")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [_usage("12", 3), _usage(True, 3), _usage(-1, 3), _usage(12, None), SimpleNamespace(), 42],
)
async def test_malformed_usage_never_fails_the_call(fake_llm, usage):
    fake_llm.script.append(_resp(text="照常返回", usage=usage))
    with usage_scope() as meter:
        assert await _call() == "照常返回"
    stat = meter.snapshot()["character"]
    assert stat.calls == 1 and stat.estimated_calls == 1


@pytest.mark.asyncio
async def test_one_retry_then_success(fake_llm):
    fake_llm.script.extend([RuntimeError("抖动"), _resp(usage=_usage(5, 1))])
    with usage_scope() as meter:
        await _call()
    stat = meter.snapshot()["character"]
    assert (stat.calls, stat.retries, stat.failures) == (1, 1, 0)


@pytest.mark.asyncio
async def test_exhausted_retries_count_as_one_failure(fake_llm):
    fake_llm.script.extend([RuntimeError("坏"), RuntimeError("坏"), RuntimeError("坏")])
    with usage_scope() as meter:
        with pytest.raises(LLMError):
            await _call()
    stat = meter.snapshot()["character"]
    assert (stat.calls, stat.retries, stat.failures) == (0, 2, 1)
    assert len(fake_llm.requests) == 3


@pytest.mark.asyncio
async def test_untagged_calls_are_visible(fake_llm):
    with usage_scope() as meter:
        await llm.chat_safe([{"role": "user", "content": "x"}])
    assert meter.snapshot()["untagged"].calls == 1


@pytest.mark.asyncio
async def test_calls_without_a_meter_still_work(fake_llm):
    assert current_meter() is None
    assert await _call() == "好"


# ---------------------------------------------------------------------------
# 归属：并发不串、嵌套独占、子任务与线程跟着走
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_scopes_do_not_mix(fake_llm):
    async def scene(n: int) -> UsageMeter:
        with usage_scope() as meter:
            for _ in range(n):
                await _call()
        return meter

    a, b = await asyncio.gather(scene(2), scene(5))
    assert a.snapshot()["character"].calls == 2
    assert b.snapshot()["character"].calls == 5


@pytest.mark.asyncio
async def test_nested_scope_takes_its_calls_exclusively(fake_llm):
    with usage_scope() as outer:
        await _call()
        with usage_scope() as inner:
            await _call(LLMPurpose.EVALUATE)
        await _call()
    assert outer.snapshot()["character"].calls == 2
    assert "evaluate" not in outer.snapshot()
    assert inner.snapshot()["evaluate"].calls == 1


@pytest.mark.asyncio
async def test_gathered_children_report_to_the_same_meter(fake_llm):
    """selector 的并发打分走 gather，子任务复制上下文，计数应汇总到同一场。"""
    with usage_scope() as meter:
        await asyncio.gather(*(_call(LLMPurpose.SELECTOR) for _ in range(3)))
    assert meter.snapshot()["selector"].calls == 3


@pytest.mark.asyncio
async def test_embedding_in_a_worker_thread_is_attributed(monkeypatch):
    from backend.memory import embeddings

    def create(model, input):
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.0]) for _ in input],
            usage=SimpleNamespace(prompt_tokens=17),
        )

    monkeypatch.setattr(
        embeddings, "OpenAI", lambda **_: SimpleNamespace(embeddings=SimpleNamespace(create=create))
    )
    fn = embeddings.RemoteEmbeddingFunction(model="m")
    with usage_scope() as meter:
        # LongTermMemory 就是这样调它的：asyncio.to_thread 复制上下文到线程
        await asyncio.to_thread(fn, ["甲", "乙"])
    stat = meter.snapshot()["embedding"]
    assert (stat.calls, stat.prompt_tokens, stat.completion_tokens) == (1, 17, 0)


@pytest.mark.asyncio
async def test_embedding_failure_is_counted_and_reraised(monkeypatch):
    from backend.memory import embeddings

    def create(model, input):
        raise RuntimeError("embedding 挂了")

    monkeypatch.setattr(
        embeddings, "OpenAI", lambda **_: SimpleNamespace(embeddings=SimpleNamespace(create=create))
    )
    fn = embeddings.RemoteEmbeddingFunction(model="m")
    with usage_scope() as meter:
        with pytest.raises(RuntimeError):
            await asyncio.to_thread(fn, ["甲"])
    assert meter.snapshot()["embedding"].failures == 1


def test_seed_accumulates_without_touching_the_source():
    base = {"character": LLMUsageStat(calls=3, prompt_tokens=30, seconds=1.5)}
    meter = UsageMeter(base)
    meter.record_call("character", prompt_tokens=10, completion_tokens=1, estimated=False, seconds=0.5)
    assert meter.snapshot()["character"].calls == 4
    assert meter.snapshot()["character"].prompt_tokens == 40
    assert base["character"].calls == 3  # 来源对象不被改写


def test_total_sums_every_purpose():
    stats = {
        "character": LLMUsageStat(calls=2, prompt_tokens=20, completion_tokens=4, seconds=1.0),
        "selector": LLMUsageStat(calls=3, prompt_tokens=9, failures=1, seconds=0.5),
    }
    t = total(stats)
    assert (t.calls, t.prompt_tokens, t.completion_tokens, t.failures) == (5, 29, 4, 1)
    assert t.seconds == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# 持久化：旧数据与坏数据
# ---------------------------------------------------------------------------


def test_deserialize_tolerates_old_and_corrupt_data():
    assert deserialize_usage(None, "旧场景") == {}
    assert deserialize_usage("坏", "场景") == {}
    out = deserialize_usage(
        {
            "character": {"calls": 2, "prompt_tokens": True, "completion_tokens": -3, "seconds": math.nan},
            "selector": "不是对象",
            "evaluate": {"calls": 1, "seconds": 2},
        },
        "场景",
    )
    assert set(out) == {"character", "evaluate"}
    assert out["character"] == LLMUsageStat(calls=2)
    assert out["evaluate"].seconds == 2.0


@pytest.mark.asyncio
async def test_usage_round_trips_through_the_repository():
    await repository.save_project(Project(project_id="proj-usage-rt", name="计数往返"))
    scene = Scene(
        scene_id="scene-usage-rt",
        project_id="proj-usage-rt",
        branch_id="b",
        llm_usage={"character": LLMUsageStat(calls=2, prompt_tokens=7, estimated_calls=1, seconds=0.25)},
    )
    await repository.save_scene(scene)
    back = await repository.get_scene("scene-usage-rt")
    assert back.llm_usage == scene.llm_usage

    ev = SceneEvaluation(scene_id="scene-usage-rt", llm_usage={"evaluate": LLMUsageStat(calls=1)})
    await repository.save_evaluation(ev)
    assert (await repository.get_evaluation("scene-usage-rt")).llm_usage == ev.llm_usage


# ---------------------------------------------------------------------------
# run_scene 集成：归属到场景、评估分开、continue 累加、两条红线
# ---------------------------------------------------------------------------


def _install_fake_run(monkeypatch, scene_id: str, *, turns: int):
    """引擎每轮真的经 llm.chat_safe 发一次角色调用；导演评估发一次评估调用。"""

    class FakeEngine:
        def __init__(self, scene_obj, *args, **kwargs):
            self.scene = scene_obj

        def inject_history(self, *args, **kwargs):
            pass

        async def run(self, on_turn=None, on_persist=None, on_after_snapshot=None, on_environment=None):
            log = list(self.scene.dialogue_log)
            for _ in range(turns):
                await _call(LLMPurpose.CHARACTER)
                turn = DialogueTurn(scene_id=self.scene.scene_id, turn_number=len(log) + 1,
                                    character_name="甲", dialogue=f"第{len(log) + 1}句")
                log.append(turn)
                self.scene.dialogue_log = list(log)
                self.scene.turns_completed = len(log)
                if on_turn:
                    await on_turn(turn)
            self.scene.status = "completed"
            return SceneResult(scene_id=self.scene.scene_id, dialogue_log=log,
                               snapshot_id_before="", snapshot_id_after="",
                               turns_completed=len(log), terminated_reason="max_turns")

    class FakeDirector:
        def __init__(self, *args, **kwargs):
            pass

        async def evaluate_scene(self, *args, **kwargs):
            await _call(LLMPurpose.EVALUATE)
            return SceneEvaluation(scene_id=scene_id)

    async def fake_build_agents(pid, cids, states=None, branch_id=""):
        return []

    monkeypatch.setattr(orchestrator, "build_character_agents", fake_build_agents)
    monkeypatch.setattr(orchestrator, "SceneEngine", FakeEngine)
    monkeypatch.setattr(orchestrator, "DirectorAgent", FakeDirector)


async def _new_scene(scene_id: str) -> Scene:
    pid = f"proj-{scene_id}"
    await repository.save_project(Project(project_id=pid, name="计数"))
    scene = Scene(scene_id=scene_id, project_id=pid, branch_id="branch-main", name="计数场", max_turns=3)
    await repository.save_scene(scene)
    return scene


@pytest.mark.asyncio
async def test_run_scene_attributes_calls_to_scene_and_evaluation_separately(fake_llm, monkeypatch):
    scene = await _new_scene("scene-usage-run")
    _install_fake_run(monkeypatch, scene.scene_id, turns=3)
    await orchestrator.run_scene(scene.scene_id)

    stored = await repository.get_scene(scene.scene_id)
    assert stored.llm_usage["character"].calls == 3
    assert "evaluate" not in stored.llm_usage
    evaluation = await repository.get_evaluation(scene.scene_id)
    assert evaluation.llm_usage["evaluate"].calls == 1
    assert "character" not in evaluation.llm_usage


@pytest.mark.asyncio
async def test_continue_accumulates_across_segments(fake_llm, monkeypatch):
    scene = await _new_scene("scene-usage-continue")
    _install_fake_run(monkeypatch, scene.scene_id, turns=2)
    await orchestrator.run_scene(scene.scene_id)
    stored = await repository.get_scene(scene.scene_id)
    stored.status = "pending"
    await repository.save_scene(stored)

    await orchestrator.run_scene(scene.scene_id)
    assert (await repository.get_scene(scene.scene_id)).llm_usage["character"].calls == 4


@pytest.mark.asyncio
async def test_counts_are_persisted_turn_by_turn(fake_llm, monkeypatch):
    """计数随逐轮落盘带走：中途崩溃也留得下已经花掉的部分。"""
    scene = await _new_scene("scene-usage-midway")
    seen: list[int] = []
    original = repository.save_scene

    async def spy(s):
        await original(s)
        if s.scene_id == scene.scene_id:
            seen.append(s.llm_usage.get("character", LLMUsageStat()).calls)

    monkeypatch.setattr(repository, "save_scene", spy)
    _install_fake_run(monkeypatch, scene.scene_id, turns=3)
    await orchestrator.run_scene(scene.scene_id)
    # 开跑落一次（0），每轮各一次（1、2、3），收尾一次（3）
    assert seen == [0, 1, 2, 3, 3]


@pytest.mark.asyncio
async def test_no_save_scene_after_the_evaluation_event(fake_llm, monkeypatch):
    """红线 R1：用户收到评估就能决策，之后再整份覆盖写会抹掉决策对场景的改动。"""
    scene = await _new_scene("scene-usage-r1")
    order: list[str] = []
    original_save, original_publish = repository.save_scene, events.publish

    async def save_spy(s):
        order.append("save")
        await original_save(s)

    async def publish_spy(sid, event, payload):
        order.append(f"event:{event}")
        await original_publish(sid, event, payload)

    monkeypatch.setattr(repository, "save_scene", save_spy)
    monkeypatch.setattr(events, "publish", publish_spy)
    _install_fake_run(monkeypatch, scene.scene_id, turns=2)
    await orchestrator.run_scene(scene.scene_id)

    assert "event:evaluation" in order
    assert "save" not in order[order.index("event:evaluation"):]


@pytest.mark.asyncio
async def test_run_scene_installs_its_own_meter_and_restores_the_caller(fake_llm, monkeypatch):
    """红线 R2：AutoPilot 的自动 continue 从上一轮的上下文里起任务，继承来的计数器不能用。"""
    scene = await _new_scene("scene-usage-r2")
    _install_fake_run(monkeypatch, scene.scene_id, turns=2)
    inherited = UsageMeter()
    token = activate(inherited)
    try:
        await orchestrator.run_scene(scene.scene_id)
        assert current_meter() is inherited  # 退出时撤下，调用方的计数器复原
    finally:
        deactivate(token)
    assert inherited.snapshot() == {}
    assert (await repository.get_scene(scene.scene_id)).llm_usage["character"].calls == 2


# ---------------------------------------------------------------------------
# 设计单 §8：record 档下纯神态动作零 LLM 调用
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pure_expression_actions_make_no_extraction_call(fake_llm):
    from backend.scene_engine.action_intents import ActionIntentExtractor

    crown = WorldObject(object_id="o-crown", project_id="p", name="王冠", public_description="水晶冠")
    extractor = ActionIntentExtractor([crown])
    with usage_scope() as meter:
        await extractor.extract(["皱眉", "叹了口气"], "伊莎贝尔")
    assert meter.snapshot() == {}

    fake_llm.script.append(_resp(text="[]", usage=_usage(5, 1)))
    with usage_scope() as meter:
        await extractor.extract(["伸手去拿王冠"], "伊莎贝尔")
    assert meter.snapshot()["action_extract"].calls == 1


# ---------------------------------------------------------------------------
# scripts/usage_report：只读汇总
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_report_sums_runs_and_evaluations(capsys):
    from scripts import usage_report

    pid = "proj-usage-report"
    await repository.save_project(Project(project_id=pid, name="报表"))
    await repository.save_scene(Scene(
        scene_id="scene-report-a", project_id=pid, branch_id="b", name="甲场",
        llm_usage={"character": LLMUsageStat(calls=4, prompt_tokens=400, completion_tokens=40),
                   "selector": LLMUsageStat(calls=2, prompt_tokens=50, estimated_calls=1)},
    ))
    # 计数字段上线前的旧场景：没有记录，不得让报表出错
    await repository.save_scene(Scene(scene_id="scene-report-old", project_id=pid, branch_id="b", name="旧场"))
    await repository.save_evaluation(SceneEvaluation(
        scene_id="scene-report-a", llm_usage={"evaluate": LLMUsageStat(calls=1, prompt_tokens=900)}
    ))

    assert await usage_report._main(pid, None) == 0
    out = capsys.readouterr().out
    assert "无记录" in out
    assert "≈" in out  # selector 有估算的调用
    total_line = next(line for line in out.splitlines() if "总计" in line)
    assert " 7 次" in total_line and "1350" in total_line


# ---------------------------------------------------------------------------
# 静态检查：每个调用点都标了用途
# ---------------------------------------------------------------------------


def test_every_llm_call_site_tags_a_purpose():
    """漏标会归进 untagged、在统计里可见；但最好在这里就拦下。"""
    missing: list[str] = []
    for path in sorted((ROOT / "backend").rglob("*.py")):
        if path.name == "llm.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in {"chat", "chat_safe"} and not any(k.arg == "purpose" for k in node.keywords):
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert missing == []
