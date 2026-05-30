<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '@/api/client'
import { useDirectorStore } from '@/stores/director'

const props = defineProps<{ projectId: string }>()
const directorStore = useDirectorStore()

const format = ref('web_novel')
const branchId = ref('')
const loading = ref(false)
const result = ref('')

const FORMATS = [
  { value: 'web_novel', label: '网络小说' },
  { value: 'screenplay', label: '影视剧本' },
  { value: 'stage_play', label: '舞台剧本' },
  { value: 'summary', label: '推演报告' },
  { value: 'raw', label: '原始日志(JSON)' },
]

onMounted(() => directorStore.loadBranches(props.projectId))

async function generate() {
  loading.value = true
  result.value = ''
  try {
    const out = await api.generateOutput(props.projectId, {
      format: format.value,
      branch_id: branchId.value || null,
      scene_ids: [],
    })
    result.value = out.content
  } catch (e) {
    result.value = `生成失败：${(e as Error).message}`
  } finally {
    loading.value = false
  }
}

function download() {
  const blob = new Blob([result.value], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `plotsystem_${format.value}.txt`
  a.click()
  URL.revokeObjectURL(url)
}

function flatten(roots: any[]): any[] {
  const out: any[] = []
  const walk = (n: any) => {
    out.push(n.branch)
    n.children.forEach(walk)
  }
  roots.forEach(walk)
  return out
}
</script>

<template>
  <div class="output">
    <h1>输出导出</h1>
    <div class="grid">
      <section class="card config">
        <div class="field">
          <label>输出格式</label>
          <select v-model="format">
            <option v-for="f in FORMATS" :key="f.value" :value="f.value">{{ f.label }}</option>
          </select>
        </div>
        <div class="field">
          <label>分支范围</label>
          <select v-model="branchId">
            <option value="">全部分支</option>
            <option
              v-for="b in flatten(directorStore.branchTree.roots)"
              :key="b.branch_id"
              :value="b.branch_id"
            >{{ b.name }}</option>
          </select>
        </div>
        <button :disabled="loading" @click="generate">{{ loading ? '生成中...' : '✨ 生成' }}</button>
        <button class="ghost" :disabled="!result" @click="download" style="margin-top: 8px">⬇ 下载</button>
      </section>

      <section class="card preview">
        <h3>预览</h3>
        <pre v-if="result" class="result">{{ result }}</pre>
        <div v-else class="dim empty">选择格式并生成，结果将显示在此处。</div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.output h1 {
  margin-bottom: 16px;
}
.grid {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 18px;
}
.config button {
  width: 100%;
}
.preview {
  min-height: 60vh;
}
.result {
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
  margin-top: 12px;
  line-height: 1.8;
}
.empty {
  padding: 60px 0;
  text-align: center;
}
</style>
