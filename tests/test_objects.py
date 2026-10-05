"""工单24 PR-1a：物件的存储、预算闸门与用户编辑。

三道闸门（设计单 §6）各钉一组用例：构建期截断、用户编辑 422 不截断、读取侧压回且不写回。
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from backend.exceptions import ConflictError, InvalidRequestError
from backend.models import CharacterCard, WorldObject
from backend.services import repository
from backend.services.objects import (
    HIDDEN_RULE_TOKENS,
    HIDDEN_RULES_BUDGET_TOKENS,
    MAX_HIDDEN_RULES,
    MAX_OBJECT_ALIASES,
    MAX_PROJECT_OBJECTS,
    OBJECT_DESC_TOKENS,
    OBJECT_NAME_CHARS,
    ObjectFields,
    apply_object_edit,
    clamp_object,
    object_id_for_request,
)
from backend.utils.llm import estimate_tokens

CHAR_NAMES = {"伊莎贝尔", "诺安"}
CHAR_IDS = {"c-isa", "c-noan"}


def _crown(**kw) -> WorldObject:
    base = dict(
        object_id="o-crown",
        project_id="p",
        name="玻璃王冠",
        aliases=["王冠"],
        public_description="一顶透明的玻璃王冠。",
        hidden_rules=["戴上后投射佩戴者最强烈的记忆"],
        visibility="global",
    )
    base.update(kw)
    return WorldObject(**base)


# ---------------------------------------------------------------------------
# clamp_object：构建期与读取侧共用
# ---------------------------------------------------------------------------


def test_clamp_keeps_a_valid_object_untouched():
    # 与同一份副本比：再调一次 _crown() 会生成新的时间戳，跨过微秒刻度就不相等
    obj = _crown()
    before = deepcopy(obj)
    assert clamp_object(obj, CHAR_NAMES) == []
    assert obj == before


def test_clamp_drops_bad_aliases():
    """单字别名、与角色同名、与本名相同、超长的别名都丢弃 —— 它们会让预过滤每轮命中。"""
    obj = _crown(aliases=["冠", "诺安", "玻璃王冠", "王冠", "王冠", "x" * 17, "那顶王冠"])
    issues = clamp_object(obj, CHAR_NAMES)
    assert obj.aliases == ["王冠", "那顶王冠"]
    assert any("单字" in i for i in issues)
    assert any("角色同名" in i for i in issues)


def test_clamp_caps_alias_count():
    obj = _crown(aliases=[f"别名{i}" for i in range(MAX_OBJECT_ALIASES + 3)])
    clamp_object(obj)
    assert len(obj.aliases) == MAX_OBJECT_ALIASES


@pytest.mark.parametrize("raw", [None, "", "public", "character:c-noan", "PRIVATE", "GLOBAL", 1, ["global"]])
def test_clamp_invalid_visibility_tightens_to_hidden(raw):
    """非法或缺失按 hidden（失败即收紧，同工单29），绝不退回 global。"""
    obj = _crown(visibility=raw)
    clamp_object(obj)
    assert obj.visibility == "hidden"


def test_clamp_keeps_every_holder_of_a_private_object():
    """A、B 知道、C 不知道：名单必须原样留下，单值表达不了（设计单 A11）。"""
    obj = _crown(visibility="private", known_by=["c-noan", "c-isa", "c-noan", " "])
    assert clamp_object(obj, CHAR_NAMES, CHAR_IDS) == []
    assert obj.visibility == "private"
    assert obj.known_by == ["c-noan", "c-isa"]


def test_clamp_drops_unknown_holders_and_tightens_when_none_left():
    obj = _crown(visibility="private", known_by=["c-noan", "c-ghost"])
    issues = clamp_object(obj, CHAR_NAMES, CHAR_IDS)
    assert obj.known_by == ["c-noan"] and any("c-ghost" in i for i in issues)

    obj = _crown(visibility="private", known_by=["c-ghost"])
    clamp_object(obj, CHAR_NAMES, CHAR_IDS)
    assert obj.visibility == "hidden" and obj.known_by == []


@pytest.mark.parametrize("raw", [[], None, "c-noan-as-string-is-ok-but-unknown", [{"id": 1}]])
def test_clamp_private_without_valid_holders_is_hidden(raw):
    obj = _crown(visibility="private", known_by=raw)
    clamp_object(obj, CHAR_NAMES, CHAR_IDS)
    assert obj.visibility == "hidden"


def test_clamp_clears_holders_when_not_private():
    """名单只属于 private；留着的话改回 private 会复活一份过时的知情者。"""
    obj = _crown(visibility="global", known_by=["c-noan"])
    clamp_object(obj, CHAR_NAMES, CHAR_IDS)
    assert obj.known_by == []


def test_clamp_squeezes_budgets_and_collapses_lines():
    obj = _crown(
        name="王" * (OBJECT_NAME_CHARS + 5),
        public_description="透明。\n" + "很长的描述" * 200,
        hidden_rules=["规则\n第二行"] + ["条" * (HIDDEN_RULE_TOKENS + 50)] * (MAX_HIDDEN_RULES + 2),
    )
    clamp_object(obj)
    assert len(obj.name) == OBJECT_NAME_CHARS
    assert estimate_tokens(obj.public_description) <= OBJECT_DESC_TOKENS + 5
    assert obj.hidden_rules[0] == "规则 第二行", "一行一条约束的是渲染结果，规则必须塌单行"
    assert len(obj.hidden_rules) <= MAX_HIDDEN_RULES
    assert sum(estimate_tokens(r) for r in obj.hidden_rules) <= HIDDEN_RULES_BUDGET_TOKENS


def test_clamp_keeps_line_breaks_in_short_description():
    """公开描述在存储里保留分段（前端是多行文本框），只有超预算时才塌掉截断。"""
    obj = _crown(public_description="第一段。\n第二段。")
    clamp_object(obj)
    assert obj.public_description == "第一段。\n第二段。"


# ---------------------------------------------------------------------------
# repository：读取侧闸门，只压不写回
# ---------------------------------------------------------------------------


async def _project_with_chars(project_id: str) -> None:
    for cid, name in [("c-isa", "伊莎贝尔"), ("c-noan", "诺安")]:
        await repository.save_character(CharacterCard(character_id=cid, project_id=project_id, name=name))


def _objects_dir(project_id: str):
    return repository._objects_dir(project_id)


@pytest.mark.asyncio
async def test_hand_edited_file_is_clamped_on_read_but_not_rewritten():
    pid = "p-obj-handedit"
    await _project_with_chars(pid)
    path = _objects_dir(pid) / "crown.json"
    raw = json.dumps(
        {
            "object_id": "something-else",
            "name": "玻璃王冠",
            "aliases": ["冠", "伊莎贝尔", "王冠"],
            "public_description": "长" * 5000,
            "hidden_rules": "只写了一条字符串",
            "visibility": "everyone",
            "known_by": ["c-noan"],
        },
        ensure_ascii=False,
    )
    path.write_text(raw, encoding="utf-8")

    obj = await repository.get_object(pid, "crown")
    assert obj.object_id == "crown", "文件名优先于 JSON 内的 object_id"
    assert obj.aliases == ["王冠"]
    assert estimate_tokens(obj.public_description) <= OBJECT_DESC_TOKENS + 5
    assert obj.hidden_rules == ["只写了一条字符串"]
    assert obj.visibility == "hidden" and obj.known_by == []
    assert path.read_text(encoding="utf-8") == raw, "读取侧只压不写回"


@pytest.mark.asyncio
async def test_private_holders_survive_round_trip_and_track_deleted_characters():
    pid = "p-obj-holders"
    await _project_with_chars(pid)
    await repository.save_object(_crown(project_id=pid, visibility="private", known_by=["c-noan", "c-isa"]))
    loaded = await repository.get_object(pid, "o-crown")
    assert (loaded.visibility, loaded.known_by) == ("private", ["c-noan", "c-isa"])

    # 角色卡没了（项目重建过）：读取侧把它从名单里拿掉，名单空了就收紧
    repository._characters_dir(pid).joinpath("c-isa.json").unlink()
    assert (await repository.get_object(pid, "o-crown")).known_by == ["c-noan"]


@pytest.mark.asyncio
async def test_bad_files_do_not_break_the_list():
    pid = "p-obj-badfiles"
    d = _objects_dir(pid)
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    (d / "array.json").write_text("[1, 2]", encoding="utf-8")
    (d / "noname.json").write_text(json.dumps({"name": "  "}), encoding="utf-8")
    await repository.save_object(_crown(project_id=pid))
    assert [o.object_id for o in await repository.list_objects(pid)] == ["o-crown"]


@pytest.mark.asyncio
async def test_non_utf8_file_is_skipped_like_any_corrupted_file():
    """记事本另存为 ANSI 就是 GBK：UnicodeDecodeError 不在 JSON / OS 错误之列，曾让整个列表五百。"""
    pid = "p-obj-gbk"
    (_objects_dir(pid) / "gbk.json").write_bytes('{"name": "王冠"}'.encode("gbk"))
    await repository.save_object(_crown(project_id=pid))
    assert [o.object_id for o in await repository.list_objects(pid)] == ["o-crown"]


@pytest.mark.asyncio
async def test_hand_written_naive_timestamp_sorts_with_system_ones():
    """手写的 `2026-01-01T00:00:00` 不带时区，与系统写的带时区时间一比较就 TypeError。"""
    pid = "p-obj-naive"
    await repository.save_object(_crown(project_id=pid))
    (_objects_dir(pid) / "hand.json").write_text(
        json.dumps({"name": "手写物件", "created_at": "2026-01-01T00:00:00"}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert [o.object_id for o in await repository.list_objects(pid)] == ["hand", "o-crown"]


@pytest.mark.asyncio
async def test_list_caps_object_count():
    pid = "p-obj-many"
    for i in range(MAX_PROJECT_OBJECTS + 3):
        await repository.save_object(WorldObject(object_id=f"o{i:03d}", project_id=pid, name=f"物件{i}"))
    assert len(await repository.list_objects(pid)) == MAX_PROJECT_OBJECTS


@pytest.mark.asyncio
async def test_object_id_cannot_escape_the_objects_dir():
    pid = "p-obj-traversal"
    await repository.save_character(CharacterCard(character_id="c1", project_id=pid, name="甲"))
    assert await repository.find_object(pid, "../characters/c1") is None
    assert await repository.delete_object(pid, "../characters/c1") is False
    assert (repository._characters_dir(pid) / "c1.json").exists()


@pytest.mark.asyncio
async def test_save_round_trip_and_atomic_write_leaves_no_temp():
    pid = "p-obj-roundtrip"
    await repository.save_object(_crown(project_id=pid, public_description="第一段。\n第二段。"))
    loaded = await repository.get_object(pid, "o-crown")
    assert loaded.public_description == "第一段。\n第二段。"
    assert loaded.hidden_rules == ["戴上后投射佩戴者最强烈的记忆"]
    assert [p.name for p in _objects_dir(pid).iterdir()] == ["o-crown.json"]
    assert await repository.delete_object(pid, "o-crown") is True
    assert await repository.delete_object(pid, "o-crown") is False


# ---------------------------------------------------------------------------
# apply_object_edit：用户写入，422 不截断 + 幂等键 + 乐观并发
# ---------------------------------------------------------------------------


def _edit(current, fields, *, base_revision=0, request_id="r1"):
    return apply_object_edit(
        current,
        fields,
        project_id="p",
        object_id=current.object_id if current else object_id_for_request("p", request_id),
        base_revision=base_revision,
        request_id=request_id,
        character_names=CHAR_NAMES,
        character_ids=CHAR_IDS,
    )


def _create_fields(**kw) -> ObjectFields:
    base = dict(
        name="玻璃王冠",
        aliases=["王冠"],
        public_description="一顶透明的玻璃王冠。",
        hidden_rules=["戴上后投射佩戴者最强烈的记忆"],
        visibility="global",
    )
    base.update(kw)
    return ObjectFields(**base)


def test_create_assigns_deterministic_id():
    obj, changed = _edit(None, _create_fields())
    assert changed
    assert obj.object_id == object_id_for_request("p", "r1")
    assert obj.revision == 0 and obj.request_id == "r1"


def test_create_replay_is_idempotent_and_reused_key_with_new_content_is_422():
    obj, _ = _edit(None, _create_fields())
    again, changed = _edit(obj, _create_fields())
    assert not changed and again is obj
    with pytest.raises(InvalidRequestError, match="幂等键"):
        _edit(obj, _create_fields(name="别的东西"))


def test_update_bumps_revision_and_checks_base_revision():
    obj, _ = _edit(None, _create_fields())
    updated, changed = _edit(obj, ObjectFields(public_description="碎了一角。"), request_id="r2")
    assert changed and updated.revision == 1
    assert updated.name == "玻璃王冠", "未给出的字段不改"
    with pytest.raises(ConflictError):
        _edit(updated, ObjectFields(public_description="又碎了一角。"), base_revision=0, request_id="r3")


def test_private_holders_are_kept_and_dropped_when_visibility_changes():
    obj, _ = _edit(None, _create_fields(visibility="private", known_by=["c-noan", "c-isa"]))
    assert obj.known_by == ["c-noan", "c-isa"]
    public, _ = _edit(obj, ObjectFields(visibility="global"), request_id="r2")
    assert public.known_by == []
    # 改回 private 不带名单 → 422，而不是悄悄复活旧名单
    with pytest.raises(InvalidRequestError, match="至少一位知情者"):
        _edit(public, ObjectFields(visibility="private"), base_revision=1, request_id="r3")


def test_update_with_same_content_is_a_noop_even_on_stale_revision():
    obj, _ = _edit(None, _create_fields())
    updated, _ = _edit(obj, ObjectFields(public_description="碎了一角。"), request_id="r2")
    same, changed = _edit(updated, ObjectFields(public_description="碎了一角。"), base_revision=0, request_id="r9")
    assert not changed and same is updated


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        (dict(name=""), "不能为空"),
        (dict(name="王" * (OBJECT_NAME_CHARS + 1)), "名称超过"),
        (dict(name="诺安"), "与角色同名"),
        (dict(aliases=["冠"]), "一个字"),
        (dict(aliases=["伊莎贝尔"]), "与角色同名"),
        (dict(aliases=[f"别名{i}" for i in range(MAX_OBJECT_ALIASES + 1)]), "别名最多"),
        (dict(public_description="长" * (OBJECT_DESC_TOKENS + 10)), "公开描述超过"),
        (dict(hidden_rules=["规"] * (MAX_HIDDEN_RULES + 1)), "隐藏规则最多"),
        (dict(hidden_rules=["长" * (HIDDEN_RULE_TOKENS + 10)]), "超过"),
        (dict(visibility="public"), "可见性"),
        (dict(visibility="character:c-noan"), "可见性"),
        (dict(visibility="private"), "至少一位知情者"),
        (dict(visibility="private", known_by=["c-ghost"]), "不是本项目的角色"),
    ],
)
def test_user_edit_rejects_instead_of_truncating(fields, message):
    with pytest.raises(InvalidRequestError, match=message):
        _edit(None, _create_fields(**fields))


# ---------------------------------------------------------------------------
# HTTP：增删改查 + 幂等 + 并发
# ---------------------------------------------------------------------------


@pytest.fixture
async def client():
    import httpx

    from backend.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


async def _http_project(pid: str) -> None:
    from backend.models import Project

    await repository.save_project(Project(project_id=pid, name=pid))
    await _project_with_chars(pid)


_BODY = {
    "name": "玻璃王冠",
    "aliases": ["王冠"],
    "public_description": "一顶透明的玻璃王冠。",
    "hidden_rules": ["戴上后投射佩戴者最强烈的记忆"],
    "visibility": "global",
    "request_id": "create-1",
}


async def test_http_create_replay_update_delete(client):
    pid = "p-obj-http"
    await _http_project(pid)
    url = f"/projects/{pid}/objects"

    first = await client.post(url, json=_BODY)
    assert first.status_code == 200
    obj = first.json()["data"]
    oid = obj["object_id"]
    assert obj["hidden_rules"] == _BODY["hidden_rules"] and obj["revision"] == 0

    # 响应丢失后原样重发：同一个物件，不新建
    replay = await client.post(url, json=_BODY)
    assert replay.json()["data"]["object_id"] == oid
    assert len((await client.get(url)).json()["data"]) == 1
    # 同一个键换了内容
    assert (await client.post(url, json={**_BODY, "name": "暗格"})).status_code == 422

    patch = {"public_description": "碎了一角。", "revision": 0, "request_id": "edit-1"}
    edited = await client.patch(f"{url}/{oid}", json=patch)
    assert edited.status_code == 200 and edited.json()["data"]["revision"] == 1
    # 编辑之后创建请求的迟到重放：仍按重放返回当前内容，不报冲突
    late = await client.post(url, json=_BODY)
    assert late.status_code == 200 and late.json()["data"]["revision"] == 1
    # PATCH 重放
    assert (await client.patch(f"{url}/{oid}", json=patch)).json()["data"]["revision"] == 1
    # 基于旧修订号的另一份编辑
    stale = await client.patch(f"{url}/{oid}", json={**patch, "public_description": "全碎了。", "request_id": "edit-2"})
    assert stale.status_code == 409
    # 超预算 422，不截断
    over = await client.patch(
        f"{url}/{oid}", json={"public_description": "长" * 1000, "revision": 1, "request_id": "edit-3"}
    )
    assert over.status_code == 422
    assert (await client.get(f"{url}/{oid}")).json()["data"]["public_description"] == "碎了一角。"

    assert (await client.delete(f"{url}/{oid}")).json()["data"]["existed"] is True
    assert (await client.delete(f"{url}/{oid}")).json()["data"]["existed"] is False
    assert (await client.get(f"{url}/{oid}")).status_code == 404


async def test_http_concurrent_creates_with_same_key_write_once(client):
    import asyncio

    pid = "p-obj-http-concurrent"
    await _http_project(pid)
    url = f"/projects/{pid}/objects"
    a, b = await asyncio.gather(client.post(url, json=_BODY), client.post(url, json=_BODY))
    assert a.status_code == b.status_code == 200
    assert a.json()["data"]["object_id"] == b.json()["data"]["object_id"]
    assert len((await client.get(url)).json()["data"]) == 1


async def test_http_concurrent_patches_one_wins_one_conflicts(client, monkeypatch):
    import asyncio

    pid = "p-obj-http-race"
    await _http_project(pid)
    url = f"/projects/{pid}/objects"
    oid = (await client.post(url, json=_BODY)).json()["data"]["object_id"]

    # 文件读写本身不让出事件循环，不插一个让出点的话两个请求根本不会交错，
    # 用例在没有锁时也会碰巧通过
    real_get = repository.get_object

    async def slow_get(*args, **kwargs):
        obj = await real_get(*args, **kwargs)
        await asyncio.sleep(0.01)
        return obj

    monkeypatch.setattr(repository, "get_object", slow_get)
    results = await asyncio.gather(
        client.patch(f"{url}/{oid}", json={"public_description": "甲改的", "revision": 0, "request_id": "a"}),
        client.patch(f"{url}/{oid}", json={"public_description": "乙改的", "revision": 0, "request_id": "b"}),
    )
    assert sorted(r.status_code for r in results) == [200, 409]


async def test_http_create_requires_existing_project_and_respects_cap(client):
    assert (await client.post("/projects/nope/objects", json=_BODY)).status_code == 404
    pid = "p-obj-http-cap"
    await _http_project(pid)
    for i in range(MAX_PROJECT_OBJECTS):
        await repository.save_object(WorldObject(object_id=f"o{i:03d}", project_id=pid, name=f"物件{i}"))
    assert (await client.post(f"/projects/{pid}/objects", json=_BODY)).status_code == 422
