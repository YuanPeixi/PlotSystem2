<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { useCharacterStore } from '@/stores/characters'
import { useSceneStore } from '@/stores/scenes'
import { useDirectorStore } from '@/stores/director'
import type { SceneConfig } from '@/types'
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

onMounted(async () => {
  await charStore.load(props.projectId)
  await directorStore.loadBranches(props.projectId)
  branchId.value = directorStore.branchTree.roots[0]?.branch.branch_id || ''
})

onBeforeUnmount(() => sceneStore.stopStream())

async function plan() {
  if (!goal.value.trim()) return
  planning.value = true
  try {
    draft.value = await sceneStore.plan(props.projectId, branchId.value, goal.value)
  } finally {
    planning.value = false
  }
}

async function startScene() {
  if (!draft.value) return
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
  await sceneStore.startSimulation(scene.scene_id)
  await directorStore.loadSnapshots(props.projectId)
}

async function onDecision(payload: Record<string, unknown>) {
  if (!sceneStore.currentScene) return
  await sceneStore.submitDecision(sceneStore.currentScene.scene_id, payload)
  await directorStore.loadBranches(props.projectId)
  await directorStore.loadSnapshots(props.projectId)
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
          <SceneTree :tree="directorStore.branchTree" @select="branchId = $event" />
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
