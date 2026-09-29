<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '@/api/client'
import type { BranchTree } from '@/types'
import PageHeader from '@/components/ui/PageHeader.vue'
import Icon from '@/components/ui/Icon.vue'

const props = defineProps<{ projectId: string }>()
const branchTree = ref<BranchTree>({ project_id: '', roots: [] })
const route = useRoute()

const format = ref('web_novel')
const branchId = ref('')
const scopeReady = ref(false)
const scopeError = ref('')
let scopeRequest = 0
const loading = ref(false)
const result = ref('')

const FORMATS = [
  { value: 'web_novel', label: '网络小说', desc: '第三人称叙事，按章节' },
  { value: 'screenplay', label: '影视剧本', desc: '场景标题、动作与对白' },
  { value: 'stage_play', label: '舞台剧本', desc: '幕、场与舞台提示' },
  { value: 'summary', label: '推演报告', desc: '评分、分支与关键决策' },
  { value: 'raw', label: '原始日志', desc: '逐轮记录的 JSON，不经改写' },
]

// 分支范围完成校验前不得生成；失效的预选不能静默扩大成全部分支。
async function loadScope() {
  const request = ++scopeRequest
  scopeReady.value = false
  scopeError.value = ''
  // 重置必须与响应一起落在竞态守卫之内：在 await 之前同步改写，会把用户
  // 刚从下拉框选好的分支静默改回路由值，点生成就导出了全部分支。
  const requested = typeof route.query.branch === 'string' ? route.query.branch : ''
  try {
    const tree = await api.getBranches(props.projectId)
    if (request !== scopeRequest) return
    branchTree.value = tree
    branchId.value = requested
    if (route.query.branch != null && typeof route.query.branch !== 'string') {
      scopeError.value = '分支参数无效，请重新选择输出范围。'
    } else if (requested && !flatten(branchTree.value.roots).some(b => b.branch_id === requested)) {
      scopeError.value = '指定分支不存在，请重新选择输出范围。'
    }
    scopeReady.value = true
  } catch {
    if (request === scopeRequest) {
      branchId.value = requested
      scopeError.value = '分支列表加载失败，请重试。'
    }
  }
}

watch(() => [props.projectId, route.query.branch], loadScope, { immediate: true })

async function generate() {
  if (!scopeReady.value || scopeError.value || loading.value) return
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
  <div class="output-page">
    <PageHeader title="输出导出" />
    <div class="out">
      <!-- 左：基础设置 -->
      <aside class="settings">
        <div class="field">
          <label for="out-branch">分支范围</label>
          <select id="out-branch" v-model="branchId" :disabled="!scopeReady" @change="scopeError = ''">
            <option value="">全部分支</option>
            <option v-for="b in flatten(branchTree.roots)" :key="b.branch_id" :value="b.branch_id">{{ b.name }}</option>
          </select>
        </div>
        <p v-if="scopeError" role="alert" class="notice danger"><Icon name="alert" :size="15" />{{ scopeError }}</p>
        <!-- 每条错误路径都要有出口：分支不存在/参数无效会把 scopeReady 置真，
             按 !scopeReady 显示等于这两种情况下没有重试按钮，而项目一个分支
             都没有时下拉框只有"全部分支"，选它不触发 change，用户彻底卡死。 -->
        <button v-if="scopeError" class="ghost retry" @click="loadScope"><Icon name="refresh" :size="15" />重试</button>

        <div class="field">
          <label>格式</label>
          <div class="formats" role="radiogroup" aria-label="输出格式">
            <button
              v-for="f in FORMATS"
              :key="f.value"
              role="radio"
              :aria-checked="format === f.value"
              @click="format = f.value"
            >
              <b>{{ f.label }}</b>
              <small>{{ f.desc }}</small>
            </button>
          </div>
        </div>
        <button class="primary generate" :disabled="loading || !scopeReady || !!scopeError" @click="generate">
          <Icon :name="loading ? 'spinner' : 'generate'" :size="15" />{{ loading ? '生成中' : '生成' }}
        </button>
      </aside>

      <!-- 中：纸面预览 -->
      <main class="preview">
        <div v-if="result" class="paper-wrap">
          <div class="paper-actions">
            <button class="ghost" @click="download"><Icon name="download" :size="15" />下载</button>
          </div>
          <pre class="paper" :class="{ mono: format === 'raw' }">{{ result }}</pre>
        </div>
        <div v-else class="empty">
          <Icon name="output" :size="28" />
          <p>选择分支范围和格式，点生成，结果会排在这里。</p>
        </div>
      </main>

      <!-- 右：预留给后续的导出能力，不挤占左侧的基础设置 -->
      <aside class="reserved-col">
        <div class="reserved">
          <b>预留区域</b>
          <p class="dim">后续新增的导出能力放在这里。</p>
          <i></i><i></i><i class="short"></i>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.output-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  container: out / inline-size;
}
.out {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 264px minmax(0, 1fr) 260px;
  grid-template-rows: minmax(0, 1fr);
}
.settings {
  border-right: 1px solid var(--line);
  padding: 16px;
  overflow: auto;
}
.retry {
  margin: -4px 0 14px;
}
.formats {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.formats button {
  height: auto;
  flex-direction: column;
  align-items: flex-start;
  gap: 0;
  padding: 8px 10px;
  border-color: transparent;
  background: transparent;
  text-align: left;
}
.formats button:hover {
  background: var(--hover);
  border-color: transparent;
}
.formats button[aria-checked='true'] {
  background: var(--panel);
  border-color: var(--line-strong);
}
.formats b {
  font-size: 13.5px;
  font-weight: 600;
}
.formats small {
  font-size: 12px;
  color: var(--ink-3);
  white-space: normal;
}
.generate {
  width: 100%;
  height: 34px;
}
.preview {
  overflow: auto;
  padding: 28px 24px 48px;
}
.paper-wrap {
  max-width: 680px;
  margin: 0 auto;
}
.paper-actions {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 8px;
}
.paper {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  padding: 48px 56px;
  font-family: var(--font-script);
  font-size: 16px;
  line-height: 1.95;
  white-space: pre-wrap;
  word-break: break-word;
}
[data-theme='dark'] .paper {
  color: #d4d6db;
}
.paper.mono {
  font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.7;
}
.empty {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--ink-2);
  text-align: center;
}
.reserved-col {
  border-left: 1px solid var(--line);
  padding: 16px;
}
.reserved {
  padding: 14px;
  border: 1px dashed var(--line-strong);
  border-radius: var(--r-md);
  font-size: 13px;
}
.reserved p {
  margin: 4px 0 12px;
}
.reserved i {
  display: block;
  height: 8px;
  margin-top: 8px;
  border-radius: 4px;
  background: var(--hover);
}
.reserved i.short {
  width: 60%;
}
@container out (max-width: 1100px) {
  .out {
    grid-template-columns: 240px minmax(0, 1fr);
  }
  .reserved-col {
    display: none;
  }
}
@container out (max-width: 760px) {
  .out {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(0, 1fr);
  }
  .settings {
    border-right: 0;
    border-bottom: 1px solid var(--line);
  }
  .formats {
    flex-direction: row;
    flex-wrap: wrap;
  }
  .formats small {
    display: none;
  }
  .paper {
    padding: 28px 22px;
  }
}
</style>
