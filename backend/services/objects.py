"""物件的规范化、预算与用户编辑（工单24）。

一层纯函数：无状态、不碰 IO、不调 LLM。与 `world_state.py` / `storyboard.py` 同一个拆分
理由 —— `repository` 读文件的那一刻就要把物件压回预算。

物件的公开描述进的是每个可见角色、每一轮的 system prompt（PR-2 起），别名决定意图抽取
预过滤的命中率（即每轮要不要多一次 LLM 调用）。三道闸门（设计单 §6）：

- 写入·构建期抽取：`clamp_object` 截断并 warning；
- 写入·用户编辑：`apply_object_edit` 超限直接 422，**不截断**（用户写的东西被悄悄改掉比报错更糟）；
- 读取：`repository` 反序列化时调 `clamp_object`，**只压不写回**。`objects/{id}.json`
  摆在项目目录里、明确支持人工编辑，写入侧的闸门管不到它（陷阱 19 的教训）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from dataclasses import dataclass

from backend.exceptions import ConflictError, InvalidRequestError
from backend.models import (
    OBJECT_VISIBILITIES,
    OBJECT_VISIBILITY_GLOBAL,
    OBJECT_VISIBILITY_HIDDEN,
    OBJECT_VISIBILITY_PRIVATE,
    WorldObject,
    now,
)
from backend.services.storyboard import normalize_memo, single_line
from backend.utils.context import ContextBudget, fit_lines
from backend.utils.llm import estimate_tokens

#: 每个项目的物件上限。物件本身不进 prompt，但 PR-2 的场景候选与预过滤都按它展开
MAX_PROJECT_OBJECTS = 40
OBJECT_NAME_CHARS = 24
MAX_OBJECT_ALIASES = 6
#: 单字别名在中文里几乎每句都能撞上（"冠""门"），预过滤每轮命中、抽取成本失控
OBJECT_ALIAS_MIN_CHARS = 2
OBJECT_ALIAS_CHARS = 16
OBJECT_DESC_TOKENS = 200
MAX_HIDDEN_RULES = 8
HIDDEN_RULE_TOKENS = 150
HIDDEN_RULES_BUDGET_TOKENS = 800
#: 每场在场物件上限。意图抽取每轮要带上候选物件的名称与公开描述，候选越多每次越贵
MAX_OBJECTS_PRESENT = 10

#: 幂等键 → object_id 的命名空间。同一项目内同一个键恒得到同一个 ID，创建请求的重放
#: 才能落在同一个文件上，而不是再建一个同名物件
_OBJECT_ID_NAMESPACE = uuid.UUID("6f1c2b1e-24a0-4c55-9b0e-0b7ec7a4e024")


def object_id_for_request(project_id: str, request_id: str) -> str:
    return str(uuid.uuid5(_OBJECT_ID_NAMESPACE, f"{project_id}\n{request_id}"))


def object_terms(objects: list[WorldObject]) -> dict[str, str]:
    """这些物件的名称与别名（按 casefold，与动作预过滤同一口径）→ 所属物件名。

    物件之间名称、别名都不得相同（PR-2a 评审）：角色说"戴上王冠"时两个"王冠"都会被预过滤
    命中、意图抽取只能随便挑一个；环境状态虽已按 object_id 归属，界面与 prompt 里仍按名称显示，
    两个同名物件的状态会被读混。
    """
    terms: dict[str, str] = {}
    for obj in objects:
        for term in (obj.name, *obj.aliases):
            if term:
                terms.setdefault(term.casefold(), obj.name)
    return terms


def duplicate_terms(objects: list[WorldObject]) -> list[str]:
    """一批物件里被不止一个物件用到的名称或别名（人工编辑的文件可能绕过写入侧校验）。"""
    owners: dict[str, set[str]] = {}
    for obj in objects:
        for term in {t.casefold() for t in (obj.name, *obj.aliases) if t}:
            owners.setdefault(term, set()).add(obj.object_id)
    return sorted(term for term, ids in owners.items() if len(ids) > 1)


def select_new_objects(
    existing: list[WorldObject], extracted: list[WorldObject]
) -> tuple[list[WorldObject], list[str]]:
    """从抽取结果里挑出项目里还没有的物件，返回 (要新建的, 因同名跳过的名称)。

    重新构建与迁移脚本共用：已有物件可能被用户手改过，按名称撞上就不覆盖。
    名称、别名任一相同都算同一件东西 —— 抽取模型这次把"王冠"当名称、上次当别名很常见。
    比较按 casefold，与写入侧校验（`object_terms`）同一口径。
    """
    taken = set(object_terms(existing))
    fresh: list[WorldObject] = []
    skipped: list[str] = []
    for obj in extracted:
        terms = {t.casefold() for t in (obj.name, *obj.aliases) if t}
        if terms & taken:
            skipped.append(obj.name)
            continue
        fresh.append(obj)
        taken.update(terms)
    return fresh, skipped


def clamp_objects_present(raw: object) -> list[str]:
    """场景里存的物件 ID 名单：去重、去空、截到上限。读取侧用，只压不报错。

    不核对物件是否还存在：物件可能在建场景之后被删，留着悬空 ID 由消费方跳过即可，
    读一次场景就去扫物件目录不值当。
    """
    ids = [i for i in dict.fromkeys(single_line(x) for x in _as_str_list(raw)) if i]
    return ids[:MAX_OBJECTS_PRESENT]


def _fit(text: str, max_tokens: int) -> str:
    return fit_lines([text], ContextBudget(max_tokens=max_tokens)).text if text else ""


def _over(text: str, max_tokens: int) -> bool:
    return bool(text) and estimate_tokens(text) > max_tokens


def normalize_visibility(raw: object) -> str:
    """非法或缺失一律按 hidden（失败即收紧，同工单29）：漏给一个物件只是角色少看见一样东西，
    错给则是把只有某人知道的东西摆到全员眼前。"""
    return raw if isinstance(raw, str) and raw in OBJECT_VISIBILITIES else OBJECT_VISIBILITY_HIDDEN


def _as_str_list(raw: object) -> list[str]:
    # 人工编辑时把单条写成字符串很常见，按一条处理，而不是整个丢掉
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(x) for x in raw if x is not None and not isinstance(x, (dict, list))]
    return []


def _clamp_aliases(
    raw: object, name: str, character_names: set[str]
) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    issues: list[str] = []
    for alias in (single_line(a) for a in _as_str_list(raw)):
        if not alias or alias == name or alias in kept:
            continue
        if len(alias) < OBJECT_ALIAS_MIN_CHARS:
            issues.append(f"单字别名「{alias}」已丢弃")
        elif len(alias) > OBJECT_ALIAS_CHARS:
            # 截短会改掉匹配语义（截出来的可能是另一个词），整条丢比截断安全
            issues.append(f"别名「{alias[:OBJECT_ALIAS_CHARS]}…」超过 {OBJECT_ALIAS_CHARS} 字，已丢弃")
        elif alias in character_names:
            issues.append(f"别名「{alias}」与角色同名，已丢弃")
        elif len(kept) >= MAX_OBJECT_ALIASES:
            issues.append(f"别名超过 {MAX_OBJECT_ALIASES} 条，「{alias}」已丢弃")
        else:
            kept.append(alias)
    return kept, issues


def _clamp_rules(raw: object) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    issues: list[str] = []
    used = 0
    for rule in (single_line(r) for r in _as_str_list(raw)):
        if not rule:
            continue
        if len(kept) >= MAX_HIDDEN_RULES:
            issues.append(f"隐藏规则超过 {MAX_HIDDEN_RULES} 条，其余已丢弃")
            break
        if _over(rule, HIDDEN_RULE_TOKENS):
            rule = _fit(rule, HIDDEN_RULE_TOKENS)
            issues.append(f"隐藏规则「{rule[:20]}…」超过 {HIDDEN_RULE_TOKENS} tokens，已截断")
        cost = estimate_tokens(rule)
        if used + cost > HIDDEN_RULES_BUDGET_TOKENS:
            issues.append(f"隐藏规则总长超过 {HIDDEN_RULES_BUDGET_TOKENS} tokens，其余已丢弃")
            break
        kept.append(rule)
        used += cost
    return kept, issues


def clamp_object(
    obj: WorldObject,
    character_names: set[str] | None = None,
    character_ids: set[str] | None = None,
) -> list[str]:
    """把一份**来历不明**的物件就地压回形状与预算，返回问题描述（供 warning）。

    读取侧与构建期共用。只压不写回 —— 读路径不该因为一次读取就改掉用户手编的文件，
    超限内容在下一次合法写入时自然收敛。公开描述在存储里保留换行（前端是多行文本框），
    预算按塌单行后的渲染形状量；其余字段一律塌单行（"一行一条"约束的是渲染结果）。

    `character_ids` 为 None 时只规整 `known_by` 的形状、不核对角色是否存在。
    """
    names = character_names or set()
    issues: list[str] = []
    name = single_line(obj.name)
    if len(name) > OBJECT_NAME_CHARS:
        issues.append(f"名称超过 {OBJECT_NAME_CHARS} 字，已截断")
        name = name[:OBJECT_NAME_CHARS]
    obj.name = name

    obj.aliases, alias_issues = _clamp_aliases(obj.aliases, name, names)
    issues.extend(alias_issues)

    desc = normalize_memo(obj.public_description if isinstance(obj.public_description, str) else "")
    if _over(single_line(desc), OBJECT_DESC_TOKENS):
        desc = _fit(single_line(desc), OBJECT_DESC_TOKENS)
        issues.append(f"公开描述超过 {OBJECT_DESC_TOKENS} tokens，已截断")
    obj.public_description = desc

    obj.hidden_rules, rule_issues = _clamp_rules(obj.hidden_rules)
    issues.extend(rule_issues)

    visibility = normalize_visibility(obj.visibility)
    if visibility != obj.visibility:
        issues.append(f"可见性 {obj.visibility!r} 非法，按 hidden 处理")
    known_by = _clean_ids(obj.known_by)
    if character_ids is not None:
        missing = [cid for cid in known_by if cid not in character_ids]
        if missing:
            issues.append(f"知情者 {'、'.join(missing)} 不是本项目的角色，已移除")
            known_by = [cid for cid in known_by if cid in character_ids]
    if visibility == OBJECT_VISIBILITY_PRIVATE and not known_by:
        issues.append("可见性为 private 但没有知情者，按 hidden 处理")
        visibility = OBJECT_VISIBILITY_HIDDEN
    obj.visibility = visibility
    # 非 private 不带名单：留着的话改回 private 会复活一份过时的知情者
    obj.known_by = known_by if visibility == OBJECT_VISIBILITY_PRIVATE else []
    return issues


def _clean_ids(raw: object) -> list[str]:
    return [cid for cid in dict.fromkeys(single_line(c) for c in _as_str_list(raw)) if cid]


# ---------------------------------------------------------------------------
# 用户编辑（422 不截断）
# ---------------------------------------------------------------------------


@dataclass
class ObjectFields:
    """一次用户写入携带的字段；为 None 的字段不改（创建时缺省为空）。"""

    name: str | None = None
    aliases: list[str] | None = None
    public_description: str | None = None
    hidden_rules: list[str] | None = None
    visibility: str | None = None
    known_by: list[str] | None = None

    def digest(self, base_revision: int) -> str:
        payload = [
            self.name, self.aliases, self.public_description, self.hidden_rules,
            self.visibility, self.known_by, base_revision,
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]


def _validated(
    obj: WorldObject,
    character_names: set[str],
    character_ids: set[str],
    taken_terms: dict[str, str] | None = None,
) -> WorldObject:
    """校验一份待写入的物件；任何超限都 422，不截断。返回规整过空白的副本。

    `taken_terms` 是**其他**物件的名称与别名（`object_terms`），撞上即 422。
    """
    taken = taken_terms or {}
    out = deepcopy(obj)
    out.name = single_line(out.name)
    if not out.name:
        raise InvalidRequestError("物件名称不能为空")
    if len(out.name) > OBJECT_NAME_CHARS:
        raise InvalidRequestError(f"物件名称超过 {OBJECT_NAME_CHARS} 字")
    if out.name in character_names:
        # 动作里提到角色名是常态，与角色同名的物件会让预过滤每轮都命中
        raise InvalidRequestError(f"物件名称「{out.name}」与角色同名")
    if out.name.casefold() in taken:
        raise InvalidRequestError(f"物件名称「{out.name}」与物件「{taken[out.name.casefold()]}」的名称或别名重复")

    aliases: list[str] = []
    for alias in (single_line(a) for a in out.aliases):
        if not alias or alias == out.name or alias in aliases:
            continue
        if len(alias) < OBJECT_ALIAS_MIN_CHARS:
            raise InvalidRequestError(f"别名「{alias}」只有一个字，会让每一轮都被当成提到了这个物件")
        if len(alias) > OBJECT_ALIAS_CHARS:
            raise InvalidRequestError(f"别名「{alias[:OBJECT_ALIAS_CHARS]}…」超过 {OBJECT_ALIAS_CHARS} 字")
        if alias in character_names:
            raise InvalidRequestError(f"别名「{alias}」与角色同名")
        if alias.casefold() in taken:
            raise InvalidRequestError(f"别名「{alias}」与物件「{taken[alias.casefold()]}」的名称或别名重复")
        aliases.append(alias)
    if len(aliases) > MAX_OBJECT_ALIASES:
        raise InvalidRequestError(f"别名最多 {MAX_OBJECT_ALIASES} 条，收到 {len(aliases)} 条")
    out.aliases = aliases

    out.public_description = normalize_memo(out.public_description)
    if _over(single_line(out.public_description), OBJECT_DESC_TOKENS):
        raise InvalidRequestError(f"公开描述超过 {OBJECT_DESC_TOKENS} tokens")

    rules = [r for r in (single_line(r) for r in out.hidden_rules) if r]
    if len(rules) > MAX_HIDDEN_RULES:
        raise InvalidRequestError(f"隐藏规则最多 {MAX_HIDDEN_RULES} 条，收到 {len(rules)} 条")
    for rule in rules:
        if _over(rule, HIDDEN_RULE_TOKENS):
            raise InvalidRequestError(f"隐藏规则「{rule[:20]}…」超过 {HIDDEN_RULE_TOKENS} tokens")
    if sum(estimate_tokens(r) for r in rules) > HIDDEN_RULES_BUDGET_TOKENS:
        raise InvalidRequestError(f"隐藏规则总长超过 {HIDDEN_RULES_BUDGET_TOKENS} tokens")
    out.hidden_rules = rules

    if out.visibility not in OBJECT_VISIBILITIES:
        raise InvalidRequestError(f"可见性 {out.visibility!r} 无效")
    if out.visibility == OBJECT_VISIBILITY_PRIVATE:
        out.known_by = _clean_ids(out.known_by)
        if not out.known_by:
            raise InvalidRequestError("可见性为 private 时必须指定至少一位知情者")
        missing = [cid for cid in out.known_by if cid not in character_ids]
        if missing:
            raise InvalidRequestError(f"知情者不是本项目的角色：{missing[0]}")
    else:
        out.known_by = []
    return out


def _content_key(obj: WorldObject) -> list:
    return [obj.name, obj.aliases, obj.public_description, obj.hidden_rules, obj.visibility, obj.known_by]


def apply_object_edit(
    current: WorldObject | None,
    fields: ObjectFields,
    *,
    project_id: str,
    object_id: str,
    base_revision: int,
    request_id: str,
    character_names: set[str],
    character_ids: set[str],
    taken_terms: dict[str, str] | None = None,
) -> tuple[WorldObject, bool]:
    """用户创建或修改物件。返回 (结果, 是否真的写了)。调用方须在项目物件锁内重读 `current`
    与 `taken_terms`（其他物件的名称与别名，`object_terms`）。

    判定顺序同分镜稿 PUT（`storyboard.apply_user_edit`）：

    1. 幂等键命中 → 重放，不写；同一个键配了不同内容 → 422（契约5）；
    2. 与当前内容完全相同 → 无操作，不先报 409；
    3. 修订号不匹配 → 409；
    4. 形状与预算不合法 → 422，不截断。

    只记最近一次写入的键：A 的响应丢了、期间又有人写了 B，A 的重试会走到第 3 步报 409 ——
    这时物件确实已经不是 A 写下的样子了，让用户看一眼再决定是对的。
    """
    digest = fields.digest(base_revision)
    if current is not None and request_id and current.request_id == request_id:
        if current.request_digest != digest:
            raise InvalidRequestError(f"幂等键 {request_id!r} 已用于另一份内容，请换一个")
        return current, False

    base = deepcopy(current) if current is not None else WorldObject(
        object_id=object_id, project_id=project_id, revision=0
    )
    if fields.name is not None:
        base.name = fields.name
    if fields.aliases is not None:
        base.aliases = list(fields.aliases)
    if fields.public_description is not None:
        base.public_description = fields.public_description
    if fields.hidden_rules is not None:
        base.hidden_rules = list(fields.hidden_rules)
    if fields.visibility is not None:
        base.visibility = fields.visibility
    if fields.known_by is not None:
        base.known_by = list(fields.known_by)

    if current is not None:
        if _content_key(_safe_normalized(base)) == _content_key(current):
            return current, False
        if base_revision != current.revision:
            raise ConflictError(
                f"物件已被修改（当前修订号 {current.revision}，请求基于 {base_revision}），请重新加载后再编辑"
            )

    result = _validated(base, character_names, character_ids, taken_terms)
    if current is not None:
        result.revision = current.revision + 1
    result.request_id = request_id
    result.request_digest = digest if request_id else ""
    result.updated_at = now()
    return result, True


def _safe_normalized(obj: WorldObject) -> WorldObject:
    """只做空白规整、不做校验的副本，用于"内容是否没变"的比较。"""
    out = deepcopy(obj)
    out.name = single_line(out.name)
    out.aliases = [a for a in dict.fromkeys(single_line(a) for a in out.aliases) if a and a != out.name]
    out.public_description = normalize_memo(out.public_description)
    out.hidden_rules = [r for r in (single_line(r) for r in out.hidden_rules) if r]
    out.known_by = _clean_ids(out.known_by) if out.visibility == OBJECT_VISIBILITY_PRIVATE else []
    return out


# ---------------------------------------------------------------------------
# 角色视野（工单20 PR-2a，设计单 A24）
# ---------------------------------------------------------------------------

#: 一个角色 system 里【在场物件】的总预算。它进这个角色整场每一轮的 prompt
OBJECTS_BRIEF_BUDGET_TOKENS = 600


def visible_to(obj: WorldObject, character_id: str) -> bool:
    """这个角色知不知道物件的存在与外观：global 全员，private 只有 `known_by`，hidden 谁都不。

    可见性按 `normalize_visibility` 再收紧一次：非法取值按 hidden（失败即收紧，同工单29）。
    """
    visibility = normalize_visibility(obj.visibility)
    if visibility == OBJECT_VISIBILITY_GLOBAL:
        return True
    return visibility == OBJECT_VISIBILITY_PRIVATE and bool(character_id) and character_id in obj.known_by


def describe_objects_for(
    objects: list[WorldObject], character_id: str, budget_tokens: int = OBJECTS_BRIEF_BUDGET_TOKENS
) -> tuple[str, list[str]]:
    """角色 system 里【在场物件】的正文与因超预算没放进去的物件名。没有可见物件时正文为空串。

    只渲染名称与**公开描述**，绝不碰 `hidden_rules`（契约1，设计单 R1）。一行一条，塌单行。
    超预算时从排在后面的物件起整条丢弃（不截半条），由调用方 warning。
    """
    lines: list[str] = []
    dropped: list[str] = []
    used = 0
    for obj in objects:
        if not visible_to(obj, character_id):
            continue
        line = f"- {single_line(obj.name)}：{single_line(obj.public_description) or '（无描述）'}"
        cost = estimate_tokens(line)
        if dropped or used + cost > budget_tokens:
            dropped.append(single_line(obj.name))
            continue
        lines.append(line)
        used += cost
    return "\n".join(lines), dropped
