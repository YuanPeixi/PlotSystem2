"""导演分镜稿的规范化、预算、渲染与合并（工单18）。

一层纯函数：无状态、不碰 IO、不调 LLM，读写两侧共用。与 `services/world_state.py`
同一个拆分理由 —— `repository` 读文件时就要压预算，而它不能 import `director_agent`
（`repository → director_agent → inspection → repository` 成环）。

四件事，每件都对应一条"不做就会静默退化"的教训：

1. **预算两道闸门**：分镜稿进的是每一次规划与评估的 prompt。写入侧由
   `merge_storyboard_patch` / `apply_user_edit` 把关，读取侧 `clamp_storyboard`
   挡住人工编辑的文件（`storyboard/{branch_id}.json` 摆在项目目录里）。
   超限要 warning 或 422，**不得静默丢弃**；
2. **渲染塌单行**：`describe_storyboard` 按"一行一条"渲染，任何进 prompt 的文本
   （标题、说明、备忘、分叉条件的键与值）都要塌成单行，否则一条能伪装成两条；
3. **基于旧稿的 patch 逐条校验**：导演的 patch 是相对它读到的那一版给出的。锁内重读
   只挡得住整份覆盖，挡不住"导演读到 v1 → 用户改成 v2 → 导演基于 v1 的改写覆盖掉
   用户"。每条操作都要核对它的前提在当前稿上仍然成立；
4. **目标版本的写回必须有确认**：只有导演（或用户）显式确认"已按当前主线目标重排"，
   `goal_revision` 才前进，且写回的是**当时看到的**版本。
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

from backend.exceptions import ConflictError, InvalidRequestError
from backend.models import (
    BeatStatus,
    ForkOrigin,
    StoryBeat,
    Storyboard,
    StoryboardChange,
    StoryboardMerge,
    StoryboardPatch,
    StoryboardSource,
    goal_revision,
)
from backend.utils.context import ContextBudget, fit_lines
from backend.utils.llm import estimate_tokens
from backend.utils.logger import get_logger

logger = get_logger("services.storyboard")

#: 节拍条数上限。已完成/已放弃的节拍也占名额，超限时优先淘汰最早了结的那些
MAX_STORY_BEATS = 20
#: 单个节拍的标题与说明上限。标题该是一句话，说明是一两句
BEAT_TITLE_TOKENS = 24
BEAT_DESC_TOKENS = 80
#: 长期备忘上限
MEMO_TOKENS = 400
#: 分叉说明（条件 + 用户备注）上限
FORK_ORIGIN_TOKENS = 200
#: 渲染进 prompt 的总预算（分叉说明 + 路线图 + 备忘）
STORYBOARD_BUDGET_TOKENS = 2000
#: changelog 保留条数。它不进 prompt，只是别让文件无限长
MAX_CHANGELOG = 50
#: changelog 单条摘要上限
_CHANGE_SUMMARY_CHARS = 200
#: 用户编辑幂等键的长度上限（与 API 校验一致）
_REQUEST_ID_CHARS = 64

_BEAT_ID_RE = re.compile(r"^b(\d+)$")
_STATUS_LABELS = {
    BeatStatus.PLANNED.value: "计划",
    BeatStatus.DONE.value: "已完成",
    BeatStatus.DROPPED.value: "已放弃",
}


# ---------------------------------------------------------------------------
# 文本形状
# ---------------------------------------------------------------------------


def single_line(value: object) -> str:
    """塌成单行。None 视为空串。"""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _fit(text: str, max_tokens: int) -> str:
    return fit_lines([text], ContextBudget(max_tokens=max_tokens)).text if text else ""


def _over(text: str, max_tokens: int) -> bool:
    return bool(text) and estimate_tokens(text) > max_tokens


def normalize_memo(value: object) -> str:
    """备忘保留换行（前端是多行文本框），只去首尾空白与行尾空白。

    塌单行留到渲染时做：存储塌掉会让用户每次保存都丢失自己的分段。
    """
    if value is None:
        return ""
    lines = [line.rstrip() for line in str(value).strip().splitlines()]
    return "\n".join(lines)


def _memo_cost(memo: str) -> int:
    return estimate_tokens(single_line(memo)) if memo else 0


def _fit_memo(memo: str) -> str:
    """备忘超预算时整体截断到预算内（渲染形状下量）。"""
    if not _over(single_line(memo), MEMO_TOKENS):
        return memo
    return _fit(single_line(memo), MEMO_TOKENS)


def beat_id(seq: int) -> str:
    return f"b{seq}"


def _beat_seq(bid: str) -> int | None:
    m = _BEAT_ID_RE.match(bid or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# 渲染（只进导演 prompt，红线 R1）
# ---------------------------------------------------------------------------


def is_goal_stale(board: Storyboard, narrative_goal: str) -> bool:
    """路线图是否对照旧版主线目标写的。空路线图没有"旧版目标"可言。"""
    return bool(board.outline) and board.goal_revision != goal_revision(narrative_goal)


def describe_fork_origin(origin: ForkOrigin | None) -> str:
    """分叉说明的确定性模板（红线 R4：分叉路径不调 LLM）。"""
    if origin is None:
        return ""
    src = single_line(origin.source_branch_name) or "来源分支"
    label = single_line(origin.source_snapshot_label) or origin.source_snapshot_id[:8]
    conditions = "；".join(
        f"{single_line(k)}={single_line(v)}" for k, v in (origin.conditions or {}).items()
        if single_line(k)
    ) or "（无，按原条件重演）"
    notes = single_line(origin.director_notes) or "（无）"
    text = f"本分支从「{src}」的快照「{label}」分叉。改变的条件：{conditions}。用户备注：{notes}"
    return _fit(text, FORK_ORIGIN_TOKENS)


def _beat_line(beat: StoryBeat) -> str:
    label = _STATUS_LABELS.get(beat.status, _STATUS_LABELS[BeatStatus.PLANNED.value])
    title = single_line(beat.title) or "（无标题）"
    desc = single_line(beat.description)
    return f"- [{beat.beat_id}][{label}] {title}" + (f"：{desc}" if desc else "")


def describe_storyboard(board: Storyboard | None, narrative_goal: str) -> str:
    """把分镜稿渲染成导演 prompt 里的一块。每条塌成单行，按"一行一条"排列。"""
    if board is None or not (board.outline or board.memo or board.fork_origin):
        return "（暂无分镜稿。路线图在第一场评估之后建立，可在 storyboard_patch 里新增节拍。）"
    lines: list[str] = []
    fork = describe_fork_origin(board.fork_origin)
    if fork:
        lines.append(f"分叉说明：{fork}")
    if board.outline:
        if is_goal_stale(board, narrative_goal):
            lines.append(
                "⚠ 以下路线图基于旧版主线目标，请据新目标重排：用 reorder / update / drop / add "
                "调整后，把 goal_realigned 设为 true"
            )
        lines.append("路线图：")
        lines.extend(_beat_line(b) for b in board.outline)
    else:
        lines.append("路线图：（空）")
    memo = single_line(board.memo)
    lines.append(f"备忘：{memo}" if memo else "备忘：（空）")
    return "\n".join(lines)


def _board_cost(board: Storyboard) -> int:
    fork = describe_fork_origin(board.fork_origin)
    cost = estimate_tokens(fork) if fork else 0
    cost += sum(estimate_tokens(_beat_line(b)) for b in board.outline)
    return cost + _memo_cost(board.memo)


def fit_storyboard(board: Storyboard) -> list[str]:
    """把分镜稿压回条数与总预算，返回被淘汰节拍的描述（供调用方 warning）。

    淘汰顺序：先淘汰**最早了结**的节拍（已完成/已放弃，路线图上最靠前的那些），
    它们对后续规划的价值最低；只剩计划中的节拍仍超限时，从末尾淘汰 —— 在合并路径上
    这等于拒绝本次最后新增的那几条。
    """
    evicted: list[str] = []
    while board.outline and (
        len(board.outline) > MAX_STORY_BEATS or _board_cost(board) > STORYBOARD_BUDGET_TOKENS
    ):
        index = next(
            (i for i, b in enumerate(board.outline) if b.status != BeatStatus.PLANNED.value),
            len(board.outline) - 1,
        )
        beat = board.outline.pop(index)
        evicted.append(f"{beat.beat_id}「{single_line(beat.title)}」")
    return evicted


# ---------------------------------------------------------------------------
# 读取侧闸门
# ---------------------------------------------------------------------------


def clamp_storyboard(board: Storyboard) -> list[str]:
    """把一份**来历不明**的分镜稿就地压回形状与预算，返回问题描述（供 warning）。

    读取侧闸门：文件可被人工编辑，也可能是立预算之前落盘的。只压不写回 ——
    结果是确定的（同一份文件每次读出同样的 ID），下一次合法写入时自然收敛。
    """
    issues: list[str] = []
    beats: list[StoryBeat] = []
    for beat in board.outline:
        title = single_line(beat.title)
        desc = single_line(beat.description)
        if not title and not desc:
            issues.append(f"空节拍 {beat.beat_id or '（无 ID）'}")
            continue
        if _over(title, BEAT_TITLE_TOKENS) or _over(desc, BEAT_DESC_TOKENS):
            issues.append(f"节拍 {beat.beat_id or title[:10]} 超长已截断")
        status = beat.status if beat.status in _STATUS_LABELS else BeatStatus.PLANNED.value
        if status != beat.status:
            issues.append(f"节拍 {beat.beat_id or title[:10]} 状态无效（{beat.status!r}）")
        beats.append(StoryBeat(
            beat_id=single_line(beat.beat_id),
            title=_fit(title, BEAT_TITLE_TOKENS) or "（无标题）",
            description=_fit(desc, BEAT_DESC_TOKENS),
            status=status,
            resolved_scene_id=single_line(beat.resolved_scene_id),
        ))

    # ID：格式不对或重复的按序补发。先把合法 ID 的最大序号纳入计数，保证不复用
    seen: set[str] = set()
    seqs = [s for s in (_beat_seq(b.beat_id) for b in beats) if s is not None]
    next_seq = max([board.next_beat_seq, *(s + 1 for s in seqs)], default=1)
    for beat in beats:
        if _beat_seq(beat.beat_id) is None or beat.beat_id in seen:
            issues.append(f"节拍 ID {beat.beat_id!r} 无效或重复，已重新分配")
            beat.beat_id = beat_id(next_seq)
            next_seq += 1
        seen.add(beat.beat_id)
    board.outline = beats
    board.next_beat_seq = max(next_seq, 1)

    memo = normalize_memo(board.memo)
    if _over(single_line(memo), MEMO_TOKENS):
        issues.append("备忘超长已截断")
        memo = _fit_memo(memo)
    board.memo = memo

    if board.fork_origin is not None:
        origin = board.fork_origin
        origin.conditions = {
            single_line(k): single_line(v) for k, v in (origin.conditions or {}).items()
            if single_line(k)
        }
        origin.director_notes = normalize_memo(origin.director_notes)

    for change in board.changelog:
        change.summary = single_line(change.summary)[:_CHANGE_SUMMARY_CHARS]
        change.request_id = single_line(change.request_id)[:_REQUEST_ID_CHARS]
    board.changelog = board.changelog[-MAX_CHANGELOG:]
    board.revision = max(0, board.revision)

    evicted = fit_storyboard(board)
    if evicted:
        issues.append("超出预算，本次读取忽略：" + "、".join(evicted))
    return issues


# ---------------------------------------------------------------------------
# 写入：changelog
# ---------------------------------------------------------------------------


def _log(board: Storyboard, source: StoryboardSource, scene_id: str, summary: str) -> None:
    board.changelog.append(
        StoryboardChange(
            source=source.value,
            scene_id=scene_id,
            summary=single_line(summary)[:_CHANGE_SUMMARY_CHARS],
        )
    )
    board.changelog = board.changelog[-MAX_CHANGELOG:]


# ---------------------------------------------------------------------------
# 写入：导演 patch
# ---------------------------------------------------------------------------


def _normalize_beat_text(beat: StoryBeat) -> tuple[str, str, bool]:
    """导演给的标题/说明：塌单行并截到单条预算内。返回 (标题, 说明, 是否截断过)。"""
    title = single_line(beat.title)
    desc = single_line(beat.description)
    truncated = _over(title, BEAT_TITLE_TOKENS) or _over(desc, BEAT_DESC_TOKENS)
    return _fit(title, BEAT_TITLE_TOKENS), _fit(desc, BEAT_DESC_TOKENS), truncated


def is_patch_empty(patch: StoryboardPatch) -> bool:
    return not (
        patch.add or patch.complete or patch.drop or patch.update
        or patch.reorder is not None or patch.memo is not None or patch.goal_realigned
        or patch.rejected
    )


def _outline_key(beats: list[StoryBeat]) -> list[tuple[str, str, str, str]]:
    """路线图的可比形状：身份、文本、状态与顺序。"""
    return [(single_line(b.beat_id), single_line(b.title), single_line(b.description), b.status)
            for b in beats]


def merge_storyboard_patch(
    current: Storyboard,
    base: Storyboard,
    patch: StoryboardPatch,
    *,
    scene_id: str,
    seen_revision: str,
) -> StoryboardMerge:
    """把导演的 patch 合并进**当前**分镜稿（调用方须在分支锁内重读 `current`）。

    `base` 是评估 prompt 里导演实际看到的那一版，`seen_revision` 是那一刻的主线目标
    版本。每条操作都核对前提在 `current` 上仍然成立，不成立就跳过这一条、其余照常：

    - 节拍只按 `beat_id` 定位。导演没见过的 ID（`base` 里没有）一律当未知处理 ——
      哪怕当前稿里恰好有同名 ID（别人后加的），它也不该被一个没见过它的导演改动；
    - `memo` 改写只在当前备忘仍等于导演读到的那份时生效；
    - `reorder` 必须是导演读到的全部计划中节拍的完整排列，且当前稿计划中节拍的**顺序**
      仍等于导演读到的（已经是导演要的顺序则按幂等处理）；先于其余操作应用（它描述的
      是导演读到的那一版的顺序）；
    - `goal_realigned` 是对导演读到的那一版路线图的判断：路线图之后被别人改过、本次有
      操作被跳过（含解析器丢弃的格式无效操作 `patch.rejected`）、或计划中节拍被预算淘汰，
      确认都不成立 —— 否则旧路线图会被标成
      已适配新目标。成立时写回 `seen_revision` 而不是最新目标：LLM 调用期间用户又改了
      目标的话，下一场仍应提示过期。

    用户↔导演、导演↔导演（同分支两场并发）走的是同一套规则。
    """
    merged = deepcopy(current)
    applied: list[str] = []
    # 解析器丢掉的格式无效操作也算被跳过：它们同样意味着导演想改的没改成
    skipped: list[str] = [f"格式无效：{r}" for r in patch.rejected]
    base_beats = {b.beat_id: b for b in base.outline}
    was_empty = not merged.outline
    roadmap_unchanged = _outline_key(current.outline) == _outline_key(base.outline)

    def target(bid: str, op: str) -> tuple[StoryBeat | None, StoryBeat | None]:
        bid = single_line(bid)
        if bid not in base_beats:
            skipped.append(f"{op} {bid or '（空 ID）'}：未知节拍")
            return None, None
        beat = next((b for b in merged.outline if b.beat_id == bid), None)
        if beat is None:
            skipped.append(f"{op} {bid}：节拍已被删除")
        return beat, base_beats[bid]

    # 1. 重排：描述的是导演读到的那一版的顺序，必须最先应用
    if patch.reorder is not None:
        order = [single_line(x) for x in patch.reorder]
        base_planned = [b.beat_id for b in base.outline if b.status == BeatStatus.PLANNED.value]
        current_planned = [
            b.beat_id for b in merged.outline if b.status == BeatStatus.PLANNED.value
        ]
        if len(set(order)) != len(order) or sorted(order) != sorted(base_planned):
            skipped.append("重排：不是计划中节拍的完整排列（缺、多或重复）")
        elif order == current_planned:
            pass  # 已经是导演要的顺序（别人排好了，或导演没改顺序）：幂等
        elif current_planned != base_planned:
            # 比的是顺序不只是集合：用户刚调好的顺序不能被基于旧稿的重排悄悄覆盖
            skipped.append("重排：路线图的计划中节拍已被他人调整")
        else:
            by_id = {b.beat_id: b for b in merged.outline}
            queue = iter(order)
            merged.outline = [
                by_id[next(queue)] if b.status == BeatStatus.PLANNED.value else b
                for b in merged.outline
            ]
            applied.append("重排路线图")

    # 2. 完成 / 放弃
    for op, status, ids in (
        ("完成", BeatStatus.DONE.value, patch.complete),
        ("放弃", BeatStatus.DROPPED.value, patch.drop),
    ):
        for bid in ids:
            beat, seen = target(bid, op)
            if beat is None or seen is None:
                continue
            if seen.status != BeatStatus.PLANNED.value:
                skipped.append(f"{op} {beat.beat_id}：导演读到时它已不是计划状态")
            elif beat.status == status:
                continue  # 别人刚做了同样的事：幂等
            elif beat.status != BeatStatus.PLANNED.value:
                skipped.append(
                    f"{op} {beat.beat_id}：节拍已被改为{_STATUS_LABELS.get(beat.status, beat.status)}"
                )
            else:
                beat.status = status
                beat.resolved_scene_id = scene_id
                applied.append(f"{op} {beat.beat_id}")

    # 3. 改写标题 / 说明
    for change in patch.update:
        beat, seen = target(change.beat_id, "改写")
        if beat is None or seen is None:
            continue
        if (beat.title, beat.description) != (seen.title, seen.description):
            skipped.append(f"改写 {beat.beat_id}：节拍已被他人修改")
            continue
        title, desc, truncated = _normalize_beat_text(change)
        if truncated:
            logger.warning("导演改写的节拍 %s 超出单条预算，已截断", beat.beat_id)
        new_title = title or beat.title
        new_desc = desc or beat.description
        if (new_title, new_desc) != (beat.title, beat.description):
            beat.title, beat.description = new_title, new_desc
            applied.append(f"改写 {beat.beat_id}")

    # 4. 新增：ID 由后端分配，删除过的序号不复用
    for new in patch.add:
        title, desc, truncated = _normalize_beat_text(new)
        if not title:
            skipped.append("新增：缺少标题")
            continue
        if truncated:
            logger.warning("导演新增的节拍「%s」超出单条预算，已截断", title)
        bid = beat_id(merged.next_beat_seq)
        merged.next_beat_seq += 1
        merged.outline.append(StoryBeat(beat_id=bid, title=title, description=desc))
        applied.append(f"新增 {bid}")
    ops_skipped = bool(skipped)

    # 5. 备忘
    if patch.memo is not None:
        memo = normalize_memo(patch.memo)
        if _over(single_line(memo), MEMO_TOKENS):
            logger.warning("导演改写的备忘超出预算（%d tokens），已截断", MEMO_TOKENS)
            memo = _fit_memo(memo)
        if merged.memo != base.memo:
            skipped.append("改写备忘：备忘已被他人修改")
        elif memo != merged.memo:
            merged.memo = memo
            applied.append("改写备忘")

    # 6. 预算：先淘汰，目标确认要看淘汰之后的路线图
    planned_before = {b.beat_id for b in merged.outline if b.status == BeatStatus.PLANNED.value}
    evicted = fit_storyboard(merged)
    planned_evicted = planned_before - {b.beat_id for b in merged.outline}

    # 7. 目标版本：只有成立的显式确认能让它前进；首次产生节拍时盖上当次看到的版本
    blockers = [
        reason for reason, hit in (
            ("路线图在导演读取后已被他人修改", not roadmap_unchanged),
            ("本次有操作被跳过或格式无效", ops_skipped),
            ("计划中节拍被预算淘汰", bool(planned_evicted)),
        ) if hit
    ]
    if patch.goal_realigned and blockers:
        skipped.append("确认已按当前主线目标重排：" + "、".join(blockers))
    if patch.goal_realigned and not blockers:
        if merged.goal_revision != seen_revision:
            merged.goal_revision = seen_revision
            applied.append("确认已按当前主线目标重排")
    elif was_empty and merged.outline:
        merged.goal_revision = seen_revision

    if applied or evicted:
        merged.revision += 1
        summary = "；".join(applied) or "（仅预算淘汰）"
        if evicted:
            summary += "；超出预算淘汰：" + "、".join(evicted)
        _log(merged, StoryboardSource.DIRECTOR, scene_id, summary)
    if skipped:
        _log(merged, StoryboardSource.DIRECTOR, scene_id, "跳过：" + "；".join(skipped))
    return StoryboardMerge(
        storyboard=merged, applied=applied, skipped=skipped, evicted=evicted
    )


# ---------------------------------------------------------------------------
# 写入：用户整份替换
# ---------------------------------------------------------------------------


def _content_equal(board: Storyboard, outline: list[StoryBeat], memo: str) -> bool:
    return _outline_key(board.outline) == _outline_key(outline) and board.memo == normalize_memo(memo)


def _request_digest(
    outline: list[StoryBeat], memo: str, base_revision: int, confirm_goal: bool,
    seen_goal_revision: str,
) -> str:
    payload = [_outline_key(outline), normalize_memo(memo), base_revision, confirm_goal,
               seen_goal_revision]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def apply_user_edit(
    current: Storyboard,
    outline: list[StoryBeat],
    memo: str,
    *,
    base_revision: int,
    confirm_goal: bool,
    current_goal_revision: str,
    seen_goal_revision: str = "",
    request_id: str = "",
) -> tuple[Storyboard, bool]:
    """用户整份替换路线图与备忘（§3.6）。返回 (结果, 是否真的写了)。

    调用方须在分支锁内重读 `current`。判定顺序有讲究：

    1. **幂等键命中 → 重放**（契约5），返回当前稿、不写。响应丢失时客户端拿不到新节拍
       分配到的 ID，只能原样重发，这份请求与当前内容永远判不等，不认键就只会误报 409。
       同一个键配了不同内容 → 422；
    2. 内容与当前完全相同（且不需要确认目标）→ 无操作，同样不先报 409；
    3. 修订号不匹配 → 409（`ConflictError`）；
    4. 形状与预算不合法 → 422，**不静默截断**：用户写的东西被悄悄改掉比报错更糟。

    确认"已按当前目标重排"写回的是 `seen_goal_revision`（用户读取时看到的目标版本），
    不是写入这一刻的最新版本：期间目标被另一个页面改掉的话，确认的仍是旧的那个，
    写回最新版本就把旧稿标成了适配一个用户没看过的目标。
    """
    seen_goal_revision = single_line(seen_goal_revision)
    if confirm_goal and not seen_goal_revision:
        raise InvalidRequestError("确认已按当前主线目标重排时必须带上读取时看到的目标版本")
    digest = _request_digest(outline, memo, base_revision, confirm_goal, seen_goal_revision)
    if request_id:
        earlier = next(
            (c for c in reversed(current.changelog)
             if c.source == StoryboardSource.USER.value and c.request_id == request_id),
            None,
        )
        if earlier is not None:
            if earlier.request_digest != digest:
                raise InvalidRequestError(f"幂等键 {request_id!r} 已用于另一份内容，请换一个")
            return current, False

    wants_goal = confirm_goal and current.goal_revision != seen_goal_revision
    if _content_equal(current, outline, memo) and not wants_goal:
        return current, False
    if base_revision != current.revision:
        raise ConflictError(
            f"分镜稿已被修改（当前修订号 {current.revision}，请求基于 {base_revision}），请重新加载后再编辑"
        )

    existing = {b.beat_id: b for b in current.outline}
    if len(outline) > MAX_STORY_BEATS:
        raise InvalidRequestError(f"路线图最多 {MAX_STORY_BEATS} 个节拍，收到 {len(outline)} 个")
    seen: set[str] = set()
    board = deepcopy(current)
    beats: list[StoryBeat] = []
    for beat in outline:
        bid = single_line(beat.beat_id)
        title = single_line(beat.title)
        desc = single_line(beat.description)
        if bid:
            if bid not in existing:
                raise InvalidRequestError(f"节拍 ID {bid!r} 不存在；新节拍请不要带 ID")
            if bid in seen:
                raise InvalidRequestError(f"节拍 ID {bid!r} 重复")
            seen.add(bid)
        if not title:
            raise InvalidRequestError("节拍标题不能为空")
        if beat.status not in _STATUS_LABELS:
            raise InvalidRequestError(f"节拍状态 {beat.status!r} 无效")
        if _over(title, BEAT_TITLE_TOKENS):
            raise InvalidRequestError(f"节拍标题「{title[:20]}…」超过 {BEAT_TITLE_TOKENS} tokens")
        if _over(desc, BEAT_DESC_TOKENS):
            raise InvalidRequestError(f"节拍「{title[:20]}」的说明超过 {BEAT_DESC_TOKENS} tokens")
        if not bid:
            bid = beat_id(board.next_beat_seq)
            board.next_beat_seq += 1
            resolved = ""
        else:
            old = existing[bid]
            # 用户把节拍改回计划中时，旧的"在哪一场了结"就不再成立
            resolved = old.resolved_scene_id if beat.status == old.status else ""
        beats.append(StoryBeat(
            beat_id=bid, title=title, description=desc, status=beat.status,
            resolved_scene_id=resolved,
        ))
    new_memo = normalize_memo(memo)
    if _over(single_line(new_memo), MEMO_TOKENS):
        raise InvalidRequestError(f"备忘超过 {MEMO_TOKENS} tokens")

    was_empty = not board.outline
    board.outline = beats
    board.memo = new_memo
    if _board_cost(board) > STORYBOARD_BUDGET_TOKENS:
        raise InvalidRequestError(
            f"分镜稿总长超过 {STORYBOARD_BUDGET_TOKENS} tokens，请精简路线图或备忘"
        )
    changes = [f"路线图 {len(beats)} 个节拍"]
    if new_memo != current.memo:
        changes.append("备忘已改")
    if wants_goal:
        board.goal_revision = seen_goal_revision
        changes.append("确认已按当前主线目标重排")
    elif was_empty and beats:
        board.goal_revision = seen_goal_revision or current_goal_revision
    board.revision += 1
    _log(board, StoryboardSource.USER, "", "用户编辑：" + "；".join(changes))
    board.changelog[-1].request_id = request_id
    board.changelog[-1].request_digest = digest if request_id else ""
    return board, True


# ---------------------------------------------------------------------------
# 分叉
# ---------------------------------------------------------------------------


def fork_storyboard(
    copy: Storyboard | None, *, project_id: str, branch_id: str, origin: ForkOrigin
) -> Storyboard:
    """由快照里的时点副本生成新分支的分镜稿（§3.5）。

    `copy is None`（本功能上线前的旧快照）时以空稿起步 —— 调用方负责 warning，
    这里**不**回读来源分支的当前分镜稿：那是分叉之后才写的，会越过继承边界。
    节拍 ID 与分配计数原样沿用，同一谱系上的 ID 不会撞车。
    """
    board = deepcopy(copy) if copy is not None else Storyboard()
    board.project_id = project_id
    board.branch_id = branch_id
    board.fork_origin = deepcopy(origin)
    board.revision += 1
    _log(board, StoryboardSource.FORK, "", describe_fork_origin(origin))
    return board
