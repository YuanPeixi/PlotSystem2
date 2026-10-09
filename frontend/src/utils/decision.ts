// 舞台底栏的导演建议（纯函数）。不 import 任何运行时模块：测试直接转译这个文件执行。
import type { SceneEvaluation } from '../types'

export type QuickDecision = 'continue' | 'next_scene' | 'rollback'
const DECISIONS: readonly string[] = ['continue', 'next_scene', 'rollback']

/** 评估未生成：四项分数为 -1（陷阱 14）。不能拿它给建议，否则一次失败的 LLM 调用会伪装成导演意见。 */
export function isEvaluationUnavailable(evaluation: Pick<SceneEvaluation, 'narrative_goal_score'>): boolean {
  return evaluation.narrative_goal_score < 0
}

/**
 * 底栏高亮哪个决策按钮、旁边写什么。有建议时只高亮、不写字；评估未到 / 不可用时写一句、不高亮。
 * 评估不属于这一场（切场景时 store 还没换过来）按"评估中"处理。
 */
export function stageDecisionHint(
  evaluation: Pick<SceneEvaluation, 'scene_id' | 'narrative_goal_score' | 'recommended_decision'> | null,
  sceneId: string,
): { recommended: QuickDecision | ''; note: string } {
  if (!evaluation || evaluation.scene_id !== sceneId) return { recommended: '', note: '评估中…' }
  if (isEvaluationUnavailable(evaluation)) return { recommended: '', note: '评估不可用' }
  const r = evaluation.recommended_decision
  return { recommended: DECISIONS.includes(r) ? (r as QuickDecision) : '', note: '' }
}
