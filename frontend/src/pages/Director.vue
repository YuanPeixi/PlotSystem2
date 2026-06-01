<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useCharacterStore } from '@/stores/characters'
import { useSceneStore } from '@/stores/scenes'
import { useDirectorStore } from '@/stores/director'
import type { Scene, SceneConfig } from '@/types'
import SceneTree from '@/components/SceneTree.vue'
import DialogLog from '@/components/DialogLog.vue'
import DirectorPanel from '@/components/DirectorPanel.vue'

const props = defineProps<{ projectId: string }>()

const charStore = useCharacterStore()
const sceneStore = useSceneStore()
const directorStore = useDirectorStore()

const goal = ref('')
const planning = ref(false)
const draft = ref<SceneConfig | null>(null)
const branchId = ref('')
// 标记 draft 是否对应已经创建好的场景（来自下一场决策）。
// 此时点击"开始模拟"应当复用已有 scene 而非再创建。
const draftSceneId = ref<string | null>(null)

function sceneToDraft(scene: Scene): SceneConfig {
  return {
    name: scene.name,
    description: scene.description,
    participating_characters: [...scene.participating_characters],
    location: scene.location,
    initial_conditions: { ...scene.initial_conditions },
    max_turns: scene.max_turns,
    speaker_mode: (scene.initial_conditions?.speaker_mode as string) || 'round_robin',
    opening_narration: (scene.initial_conditions?.opening_narration as string) || '',
  }
}

onMounted(async () => {
  await charStore.load(props.projectId)
  await directorStore.refreshAll(props.projectId)
  branchId.value = directorStore.branchTree.roots[0]?.branch.branch_id || ''
  // 刷新恢复：若 localStorage 中存有正在追踪的 scene_id，则从后端拉回完整状态
  const tracked = sceneStore.getTrackedSceneId()
  if (tracked) {
    try {
      const scene = await sceneStore.restoreScene(tracked)
      if (scene.branch_id) branchId.value = scene.branch_id
    } catch {
      /* 场景已被删除等情况，静默忽略 */
    }
  }
})

async function onSelectScene(sceneId: string) {
  // 点击树中的场景节点：加载场景元信息 + 日志 + 评估（不重新触发模拟）
  try {
    await sceneStore.restoreScene(sceneId)
  } catch {
    /* ignore */
  }
}

// 当 currentScene 变化（例如下一场决策后台创建了新场景），把它的字段填到左侧规划区
watch(
  () => sceneStore.currentScene?.scene_id,
  (newId) => {
    const scene = sceneStore.currentScene
    if (!scene) return
    // 仅当场景尚未开始（pending）或刚完成时把"规划"反填到草稿；
    // running 状态时不覆盖用户当前的编辑。
    if (scene.status === 'pending' || scene.status === 'completed') {
      draft.value = sceneToDraft(scene)
      draftSceneId.value = scene.scene_id
      if (scene.branch_id) branchId.value = scene.branch_id
    } else {
      draftSceneId.value = newId ?? null
    }
  },
)

// 每当一个场景跑完，自动刷新分支/场景/快照，让分支树显示最新状态
watch(
  () => sceneStore.completionTick,
  () => {
    directorStore.refreshAll(props.projectId).catch(() => {})
  },
)

onBeforeUnmount(() => sceneStore.stopStream())

async function plan() {
  if (!goal.value.trim()) return
  planning.value = true
  try {
    draft.value = await sceneStore.plan(props.projectId, branchId.value, goal.value)
    draftSceneId.value = null // 全新规划，未创建场景
  } finally {
    planning.value = false
  }
}

async function startScene() {
  if (!draft.value) return
  let sceneId: string
  if (draftSceneId.value) {
    // 后端已通过"下一场"决策创建了 scene；用户在前端可能做了修改（暂未支持 PATCH），
    // 这里以已创建的 scene 为准启动模拟。
    sceneId = draftSceneId.value
    await sceneStore.joinScene(sceneId)
  } else {
    const scene = await sceneStore.createScene(props.projectId, {
      branch_id: branchId.value,
      name: draft.value.name,
      description: draft.value.description,
      participating_characters: draft.value.participating_characters,
      location: draft.value.location,
      initial_conditions: draft.value.initial_conditions,
      max_turns: draft.value.max_turns,
      opening_narration: draft.value.opening_narration,
      speaker_mode: draft.value.speaker_mode || 'round_robin',
    })
    sceneId = scene.scene_id
    await sceneStore.startSimulation(sceneId)
    draftSceneId.value = sceneId
  }
  await directorStore.refreshAll(props.projectId)
}

async function onDecision(payload: Record<string, unknown>) {
  if (!sceneStore.currentScene) return
  await sceneStore.submitDecision(sceneStore.currentScene.scene_id, payload)
  await directorStore.refreshAll(props.projectId)
}
</script>

<template>
  <div class="director">
    <h1>导演视角</h1>
    <div class="layout-grid">
      <!-- 左侧：分支树 + 规划 -->
      <section class="left">
        <div class="card">
          <h3>分支树</h3>
          <SceneTree
            :tree="directorStore.branchTree"
            :scenes="directorStore.scenes"
            :selected-branch-id="branchId"
            :selected-scene-id="sceneStore.currentScene?.scene_id || ''"
            @select="branchId = $event"
            @select-scene="onSelectScene"
          />
        </div>
        <div class="card">
          <h3>规划场景</h3>
          <div class="field" style="margin-top: 10px">
            <label>叙事目标</label>
            <textarea v-model="goal" placeholder="例如：让两位主角在雨夜的酒馆中第一次正面冲突"></textarea>
          </div>
          <button :disabled="planning" @click="plan">{{ planning ? '规划中...' : '🎬 让导演规划' }}</button>

          <div v-if="draft" class="draft">
            <div class="field">
              <label>场景名</label>
              <input v-model="draft.name" />
            </div>
            <div class="field">
              <label>地点</label>
              <input v-model="draft.location" />
            </div>
            <div class="field">
              <label>描述</label>
              <textarea v-model="draft.description"></textarea>
            </div>
            <div class="field">
              <label>开场白</label>
              <textarea v-model="draft.opening_narration"></textarea>
            </div>
            <div class="field">
              <label>参与角色</label>
              <div class="char-pills">
                <span class="tag" v-for="cid in draft.participating_characters" :key="cid">
                  {{ charStore.nameOf(cid) }}
                </span>
              </div>
            </div>
            <button :disabled="sceneStore.running" @click="startScene">
              {{ sceneStore.running ? '模拟中...' : '▶ 开始模拟' }}
            </button>
          </div>
        </div>
      </section>

      <!-- 中：对话日志 -->
      <section class="card center">
        <div class="row" style="justify-content: space-between">
          <h3>{{ sceneStore.currentScene?.name || '对话日志' }}</h3>
          <span class="tag">{{ sceneStore.statusMsg || '空闲' }}</span>
        </div>
        <DialogLog :turns="sceneStore.turns" />
      </section>

      <!-- 右：导演面板 -->
      <section class="right">
        <DirectorPanel
          :evaluation="sceneStore.evaluation"
          :scene-id="sceneStore.currentScene?.scene_id || ''"
          @decision="onDecision"
        />
      </section>
    </div>
  </div>
</template>

<style scoped>
.director h1 {
  margin-bottom: 16px;
}
.layout-grid {
  display: grid;
  grid-template-columns: 320px 1fr 320px;
  gap: 16px;
  height: calc(100vh - 120px);
}
.left {
  display: flex;
  flex-direction: column;
  gap: 16px;
  overflow-y: auto;
}
.center {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.draft {
  margin-top: 16px;
  border-top: 1px solid var(--border);
  padding-top: 14px;
}
.char-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
</style>
