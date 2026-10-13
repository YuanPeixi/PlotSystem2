"""Unified world domain: immutable facts, explicit changes and historical perception.

No IO. Prompt budgets apply to projections, never to the authoritative world.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import datetime

from backend.exceptions import InvalidRequestError
from backend.models import (
    WorldFact,
    WorldObject,
    WorldObservation,
    WorldOperation,
    WorldRevision,
)
from backend.services.objects import visible_to
from backend.utils.llm import estimate_tokens

SUBJECT_KINDS = {"world", "object", "location"}
VISIBILITIES = {"global", "observable", "hidden"}
MAX_OPERATIONS = 32
MAX_VALUE_CHARS = 4000


def fact_key(kind: str, subject_id: str, attribute: str) -> str:
    return json.dumps([kind, subject_id, attribute], ensure_ascii=False, separators=(",", ":"))


def parse_operations(raw: object) -> list[WorldOperation]:
    """Strict admission: invalid input rejects the command, without truncating facts."""
    if not isinstance(raw, list) or len(raw) > MAX_OPERATIONS:
        raise InvalidRequestError("世界变化必须是数组，且一次最多 32 项")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise InvalidRequestError("世界变化的每项必须是对象")
        allowed = {"op", "subject_kind", "subject_id", "attribute", "label", "value", "visibility"}
        if set(item) - allowed:
            raise InvalidRequestError("世界变化包含未知字段")
        operation = WorldOperation(**item)
        validate_operation(operation)
        out.append(operation)
    keys = [fact_key(o.subject_kind, o.subject_id, o.attribute) for o in out]
    if len(keys) != len(set(keys)):
        raise InvalidRequestError("同一次提交不能重复修改同一属性")
    return out


def validate_operation(operation: WorldOperation) -> None:
    if not isinstance(operation.op, str) or operation.op not in {"set", "unset"}:
        raise InvalidRequestError("世界变化只支持 set / unset")
    if (
        not isinstance(operation.subject_kind, str)
        or operation.subject_kind not in SUBJECT_KINDS
        or not isinstance(operation.visibility, str)
        or operation.visibility not in VISIBILITIES
    ):
        raise InvalidRequestError("世界变化的对象类型或可见性不合法")
    for value, limit in (
        (operation.subject_id, 128),
        (operation.attribute, 64),
        (operation.label, 80),
    ):
        if not isinstance(value, str) or len(value) > limit or "\n" in value or "\r" in value:
            raise InvalidRequestError("世界变化的标识或标签格式不合法")
    if not operation.subject_id.strip() or not operation.attribute.strip():
        raise InvalidRequestError("世界变化缺少稳定对象标识或属性标识")
    if operation.subject_kind == "world" and operation.subject_id != "world":
        raise InvalidRequestError("世界层的对象标识必须是 world")
    value = operation.value
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidRequestError("世界属性不能是非有限数字")
    if isinstance(value, dict):
        if (
            set(value) != {"kind", "id"}
            or not isinstance(value["kind"], str)
            or value["kind"] not in {"character", "object", "location"}
        ):
            raise InvalidRequestError("实体引用必须包含合法的 kind 与 id")
        if not isinstance(value["id"], str) or not value["id"] or len(value["id"]) > 128:
            raise InvalidRequestError("实体引用缺少有效 id")
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise InvalidRequestError("世界属性必须是标量或实体引用")
    if len(json.dumps(value, ensure_ascii=False)) > MAX_VALUE_CHARS:
        raise InvalidRequestError("世界属性值超过存储上限")


def apply_operations(
    revision: WorldRevision,
    operations: list[WorldOperation],
    event_id: str,
    observers: list[str],
    *,
    source: str = "user",
    allowed_objects: set[str] | None = None,
    source_branch_id: str = "",
) -> WorldRevision:
    """Produce a new complete version. No stored fact is dropped to fit a prompt."""
    from backend.models import new_id, now

    operations = parse_operations([vars(o) for o in operations])
    out = deepcopy(revision)
    out.parent_revision_id = revision.revision_id
    out.revision_id = new_id()
    out.created_at = now()
    for operation in operations:
        if source == "environment":
            if operation.subject_kind != "object" or operation.subject_id not in (
                allowed_objects or set()
            ):
                raise InvalidRequestError("环境裁决只能改变本次允许作用的物件")
            if operation.visibility not in {"observable", "hidden"}:
                raise InvalidRequestError("环境属性变化只能是现场可观察状态或内部隐藏状态")
        if source == "director" and (
            operation.subject_kind != "world" or operation.visibility != "global"
        ):
            raise InvalidRequestError("导演场间命令不能覆盖已裁决的物件属性")
        if operation.subject_kind == "object" and operation.subject_id not in out.definitions:
            raise InvalidRequestError("世界变化引用了不存在的物件")
        if isinstance(operation.value, dict) and operation.value["kind"] == "object":
            if operation.value["id"] not in out.definitions:
                raise InvalidRequestError("世界属性引用了不存在的物件")
        key = fact_key(operation.subject_kind, operation.subject_id, operation.attribute)
        previous = out.facts.get(key)
        if operation.op == "unset":
            out.facts.pop(key, None)
            fact = (
                deepcopy(previous)
                if previous
                else WorldFact(
                    subject_kind=operation.subject_kind,
                    subject_id=operation.subject_id,
                    attribute=operation.attribute,
                    label=operation.label or operation.attribute,
                    visibility=operation.visibility,
                )
            )
            fact.value = None
            fact.source_event_id = event_id
            fact.source_branch_id = source_branch_id
            fact.visibility = operation.visibility
            out.removed_facts[key] = deepcopy(fact)
        else:
            fact = WorldFact(
                subject_kind=operation.subject_kind,
                subject_id=operation.subject_id,
                attribute=operation.attribute,
                label=operation.label or operation.attribute,
                value=deepcopy(operation.value),
                visibility=operation.visibility,
                source_event_id=event_id,
                source_branch_id=source_branch_id,
            )
            out.facts[key] = fact
            out.removed_facts.pop(key, None)
        # Knowledge is an observation of this version, never a subscription to future values.
        informed = [] if source == "environment" and operation.visibility == "hidden" else observers
        for observer in informed:
            out.observations.setdefault(observer, {})[key] = WorldObservation(
                fact=deepcopy(fact),
                event_id=event_id,
                deleted=operation.op == "unset",
            )
        if fact.visibility == "global":
            # Public updates invalidate older knowledge for everyone, including deletions.
            for observations in out.observations.values():
                observations.pop(key, None)
    return out


def object_state(revision: WorldRevision, object_id: str) -> dict:
    return {
        fact.attribute: deepcopy(fact.value)
        for fact in revision.facts.values()
        if fact.subject_kind == "object" and fact.subject_id == object_id
    }


def _value_text(value: object, revision: WorldRevision, character_names: dict[str, str]) -> str:
    if isinstance(value, dict):
        if value.get("kind") == "object" and value.get("id") in revision.definitions:
            return revision.definitions[value["id"]].name
        if value.get("kind") == "character":
            return character_names.get(value.get("id", ""), "某人")
    if isinstance(value, bool):
        return "是" if value else "否"
    return "未知" if value is None else " ".join(str(value).split())


def _fact_line(fact: WorldFact, revision: WorldRevision, character_names: dict[str, str]) -> str:
    subject = "世界"
    if fact.subject_kind == "object":
        definition = revision.definitions.get(fact.subject_id)
        subject = definition.name if definition else "已移除物件"
    elif fact.subject_kind == "location":
        subject = fact.subject_id
    return f"- {subject}·{fact.label or fact.attribute}：{_value_text(fact.value, revision, character_names)}"


def visible_observations(
    revision: WorldRevision,
    viewer_id: str,
    objects_present: list[str],
    location_id: str = "",
) -> tuple[list[WorldObservation], list[WorldObservation]]:
    """Current perception includes visible removals; old knowledge remains tied to its version."""
    current = []
    seen_keys = set()
    knowledge = revision.observations.get(viewer_id, {})
    for key, fact in {**revision.removed_facts, **revision.facts}.items():
        perceivable = fact.visibility == "global"
        if fact.visibility == "observable" and fact.subject_kind == "object":
            definition = revision.definitions.get(fact.subject_id)
            discovered = any(
                o.fact.subject_id == fact.subject_id and o.fact.subject_kind == "object"
                for o in knowledge.values()
            )
            perceivable = (
                fact.subject_id in objects_present
                and definition is not None
                and (visible_to(definition, viewer_id) or discovered)
            )
        elif fact.visibility == "observable" and fact.subject_kind == "location":
            perceivable = bool(location_id) and fact.subject_id == location_id
        if perceivable:
            current.append(
                WorldObservation(
                    fact=deepcopy(fact),
                    event_id=fact.source_event_id,
                    deleted=key in revision.removed_facts,
                )
            )
            seen_keys.add(key)
    remembered = [deepcopy(o) for key, o in knowledge.items() if key not in seen_keys]
    return current, remembered


def visible_facts(
    revision: WorldRevision,
    viewer_id: str,
    objects_present: list[str],
    location_id: str = "",
) -> tuple[list[WorldFact], list[WorldObservation]]:
    current, remembered = visible_observations(revision, viewer_id, objects_present, location_id)
    return [o.fact for o in current if not o.deleted], remembered


def capture_current_observations(
    revision: WorldRevision,
    viewers: list[str],
    objects_present: list[str],
    location_id: str = "",
) -> bool:
    """Persist direct perception, including removals, on a caller-owned staged version."""
    changed = False
    for viewer in viewers:
        current, _ = visible_observations(revision, viewer, objects_present, location_id)
        for observation in current:
            fact = observation.fact
            if fact.visibility == "global":
                continue
            key = fact_key(fact.subject_kind, fact.subject_id, fact.attribute)
            if revision.observations.get(viewer, {}).get(key) != observation:
                revision.observations.setdefault(viewer, {})[key] = observation
                changed = True
    return changed


def _observation_line(observation: WorldObservation, revision: WorldRevision, names: dict) -> str:
    if observation.deleted:
        subject = revision.definitions.get(observation.fact.subject_id)
        return f"- {subject.name if subject else '世界'}·{observation.fact.label or observation.fact.attribute}：已被清除"
    return _fact_line(observation.fact, revision, names)


def render_world_for_character(
    revision: WorldRevision,
    viewer_id: str,
    objects_present: list[str],
    *,
    location_id: str = "",
    character_names: dict[str, str] | None = None,
    budget_tokens: int = 1200,
) -> str:
    names = character_names or {}
    current, remembered = visible_observations(revision, viewer_id, objects_present, location_id)
    # Fresh observations first, then current facts; discard whole display lines only.
    lines = [_observation_line(o, revision, names) for o in reversed(current)]
    lines.extend(
        "此前获知（不代表当前状态）：" + _observation_line(o, revision, names)
        for o in reversed(remembered)
    )
    kept = []
    used = 0
    for line in lines:
        cost = estimate_tokens(line)
        if budget_tokens > 0 and used + cost > budget_tokens:
            continue
        kept.append(line)
        used += cost
    if len(kept) < len(lines):
        # The notice is part of the display budget as well.
        notice = "（部分已知状态因上下文预算未展示，事实仍已保存）"
        while kept and budget_tokens > 0 and used + estimate_tokens(notice) > budget_tokens:
            used -= estimate_tokens(kept.pop())
        if budget_tokens <= 0 or used + estimate_tokens(notice) <= budget_tokens:
            kept.append(notice)
    return "\n".join(kept)


def director_variables(revision: WorldRevision) -> dict[str, str]:
    """Director-only projection includes addresses so proposals can refer to stable attributes."""
    return {
        f"{fact.subject_kind}/{fact.subject_id}/{fact.attribute}": _fact_line(
            fact, revision, {}
        ).removeprefix("- ")
        for fact in revision.facts.values()
    }


def deserialize_revision(data: dict) -> WorldRevision:
    return WorldRevision(
        revision_id=data["revision_id"],
        project_id=data["project_id"],
        parent_revision_id=data.get("parent_revision_id", ""),
        facts={k: WorldFact(**v) for k, v in data.get("facts", {}).items()},
        removed_facts={k: WorldFact(**v) for k, v in data.get("removed_facts", {}).items()},
        observations={
            cid: {
                key: WorldObservation(
                    fact=WorldFact(**o["fact"]),
                    event_id=o.get("event_id", ""),
                    deleted=o.get("deleted", False),
                )
                for key, o in observations.items()
            }
            for cid, observations in data.get("observations", {}).items()
        },
        definitions={
            oid: WorldObject(
                **{
                    k: (datetime.fromisoformat(v) if k in {"created_at", "updated_at"} else v)
                    for k, v in definition.items()
                    if k in WorldObject.__dataclass_fields__
                }
            )
            for oid, definition in data.get("definitions", {}).items()
        },
        legacy_records=dict(data.get("legacy_records", {})),
        created_at=datetime.fromisoformat(data["created_at"])
        if data.get("created_at")
        else WorldRevision().created_at,
    )


def migration_preview(variables: dict, objects: list[WorldObject]) -> dict:
    """Conservative preview. Free text is never silently promoted to globally known truth."""
    entries = []
    term_ids: dict[str, set[str]] = {}
    for obj in objects:
        for term in [obj.name, *obj.aliases]:
            term_ids.setdefault(term, set()).add(obj.object_id)
    terms = {term: next(iter(ids)) for term, ids in term_ids.items() if len(ids) == 1}
    for key, value in variables.items():
        category = "unclassified"
        suggestion = None
        for term, oid in terms.items():
            prefix = term + "·"
            if key.startswith(prefix) and key[len(prefix) :]:
                category = "object_candidate"
                suggestion = vars(
                    WorldOperation(
                        subject_kind="object",
                        subject_id=oid,
                        attribute=key[len(prefix) :],
                        value=value,
                        visibility="observable",
                    )
                )
                break
        if any(word in key for word in ("已知", "知晓", "推测", "怀疑")):
            category = "knowledge_candidate"
            suggestion = None
        elif any(word in key for word in ("提及", "宣称", "声称", "公开")):
            category = "disclosure_candidate"
            suggestion = None
        entries.append({"key": key, "value": value, "category": category, "suggestion": suggestion})
    return {
        "schema_version": 2,
        "entries": entries,
        "note": "只有显式选定的属性会进入新世界；其余原样保存为旧记录，不向角色广播。",
    }
