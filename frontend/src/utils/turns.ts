// 轮次的纯函数（工单20）：SSE turn_update 的合并、计数口径。与后端 utils/turns.py 同一口径。
// 不 import 任何运行时模块：测试直接转译这个文件执行。
import type { DialogueTurn } from '../types'

/** 只认 environment 为环境回合；缺省与非法取值都按角色轮次（同后端 is_character_turn）。 */
export function isEnvironmentTurn(turn: Pick<DialogueTurn, 'kind'>): boolean {
  return turn.kind === 'environment'
}

/** 角色轮次数。max_turns 只数它：环境回合不占发言顺序（设计单 §5.5）。 */
export function characterTurnCount(turns: readonly Pick<DialogueTurn, 'kind'>[]): number {
  return turns.reduce((n, t) => (isEnvironmentTurn(t) ? n : n + 1), 0)
}

/**
 * 把一条 turn_update 合进本地日志，返回新数组；不该替换时返回 null（调用方什么都不做）。
 *
 * - 本地没有这一轮 → 忽略：终态的 reconcileLog 无条件以持久化日志为准，会补齐；
 * - revision 不比本地新 → 忽略：订阅期间铺底 GET 可能先拿到新版，排在队列里的旧事件随后才到，
 *   不比 revision 就会把新版回退成旧版（设计单 §5.2）。
 */
export function applyTurnUpdate(
  turns: readonly DialogueTurn[],
  update: DialogueTurn,
): DialogueTurn[] | null {
  const i = turns.findIndex((t) => t.turn_id === update.turn_id)
  if (i < 0) return null
  if ((update.revision ?? 0) <= (turns[i].revision ?? 0)) return null
  const next = turns.slice()
  next[i] = update
  return next
}

/**
 * `output_notice` 的标签（工单30）：后端写成 `标签：说明`，标签放角色名后，整句作悬停说明。
 * 没有冒号的（格式以后改了）退回"已截断"，至少不把整句挤进名字栏。
 */
export function outputNoticeLabel(notice: string): string {
  const i = notice.indexOf('：')
  return i > 0 && i <= 8 ? notice.slice(0, i) : '已截断'
}
