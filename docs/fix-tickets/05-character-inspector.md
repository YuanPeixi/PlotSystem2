# 工单05：新增角色 Inspect（导演视角详情）前端入口

**优先级**：P1
**预估改动范围**：小-中（纯前端，后端数据已具备）
**依赖**：建议在工单04完成后做（可复用其中完善的 `query_character_state`），但非强制，
本工单也可以只用现有的 `GET /projects/{id}/characters/{cid}` 接口独立完成。

---

## 1. 背景

CLAUDE.md 设计中，导演（Director）应该是唯一能看到角色 `unknown_facts`（该角色不知道但读者/导演知道的
信息）、完整关系网络、记忆内容的视角。后端 `GET /projects/{project_id}/characters/{char_id}` 接口
（`backend/api/characters.py`）**已经返回了包含 `unknown_facts` 的完整角色卡**，
但前端目前没有任何界面能展示这些"导演专属"信息——现有的 `CharacterCard.vue` 只在卡片上展示
`persona` 摘要和 `known_facts` 前3条标签，用户（导演）没有途径查看 `unknown_facts`、完整关系列表、
角色记忆内容。

## 2. 精确现状位置

文件：`frontend/src/components/CharacterCard.vue`
```vue
<template>
  <div class="char-card" :class="{ selected }" @click="$emit('select', character.character_id)">
    <div class="char-head">
      <div class="avatar">{{ character.name.slice(0, 1) }}</div>
      <div>
        <div class="char-name">{{ character.name }}</div>
        <div class="char-emotion dim">{{ character.current_emotion }} · {{ character.current_goal || '无明确目标' }}</div>
      </div>
    </div>
    <p class="char-persona">{{ character.persona || '（暂无设定）' }}</p>
    <div class="char-facts" v-if="character.known_facts.length">
      <span class="tag" v-for="(f, i) in character.known_facts.slice(0, 3)" :key="i">{{ f }}</span>
    </div>
  </div>
  <!-- 只有点击事件 select，没有"查看详情"入口 -->
</template>
```

后端可用但前端未使用的数据/接口：
- `GET /projects/{project_id}/characters/{char_id}` → 返回完整 `CharacterCard`（含 `unknown_facts`、
  完整 `relationships` dict、`world_lore_entries`）。已在 `frontend/src/types/index.ts` 中有对应
  TypeScript 类型 `CharacterCard`，字段齐全。
- `GET /projects/{project_id}/characters/{char_id}/memory` → 返回
  `{ short_term: string[], episodic_summary: string }`（`backend/api/characters.py` 第 44-52 行）。
  前端 `api/client.ts` 中**没有**对应封装方法。

## 3. 目标（Definition of Done）

1. **API 客户端**：在 `frontend/src/api/client.ts` 的 `api` 对象中新增：
   ```ts
   getCharacterMemory: (id: string, cid: string) =>
     unwrap<{ short_term: string[]; episodic_summary: string }>(
       http.get(`/projects/${id}/characters/${cid}/memory`),
     ),
   ```
   （`getCharacter` 已存在，可直接复用获取完整角色卡。）

2. **新建组件** `frontend/src/components/CharacterInspector.vue`（弹窗/侧拉面板形式），
   接收 `projectId` + `characterId` props，内容至少包含：
   - 基础信息（persona/appearance/speech_style，当前情绪/目标/位置）
   - **`unknown_facts` 列表**（明确用醒目样式标注"仅导演可见"，与 `known_facts` 区分展示）
   - 完整 `relationships`（遍历 dict，展示 target 角色名——需要用
     `useCharacterStore().nameOf(targetId)` 转换 ID 为名字、relation_type、strength、notes）
   - 短期记忆缓冲（`short_term` 数组，最近对话文本列表）
   - 事件摘要记忆（`episodic_summary` 文本）
   - `world_lore_entries`（该角色能感知的世界观条目列表，可选展示）

3. **接入组件**：在 `frontend/src/pages/Director.vue`（或 `Workspace.vue`，任选一个更符合当前
   角色管理入口的页面，建议 `Director.vue` 因为这是导演视角）为每个角色卡增加一个
   "🔍 详情" 按钮，点击后打开 `CharacterInspector.vue`（可用简单的 `v-if` 控制显隐 + 记录
   当前 `inspectingCharacterId`，不强制使用 UI 库的 Modal 组件，保持和现有项目风格一致的
   自定义浮层即可，参考 `DirectorPanel.vue` 中 `rollback-box` 的浮层样式模式）。

4. 样式风格需与项目暗色主题保持一致（参考 `frontend/src/styles/global.css` 中的
   `--bg`/`--card`/`--accent`/`--highlight`/`--border` 变量）。

## 4. 涉及文件

- `frontend/src/api/client.ts`（新增 `getCharacterMemory` 方法）
- `frontend/src/components/CharacterInspector.vue`（新建）
- `frontend/src/pages/Director.vue`（接入按钮 + 弹窗状态管理）
- 可能需要在 `frontend/src/stores/characters.ts` 新增一个方法获取单个角色最新详情
  （目前 store 只有批量 `load`，可复用 `api.getCharacter` 直接在组件内调用即可，不强制改 store）

## 5. 验收方式

1. 前端进入导演页面，角色列表中点击某个角色的"详情"按钮，确认弹窗正确展示：
   - `unknown_facts`（用与 `known_facts` 视觉区分的方式展示，且明确标注"仅导演可见"字样）；
   - 完整关系列表（角色名而非裸 ID）；
   - 短期记忆和事件摘要文本。
2. 确认该入口**不会**出现在任何"角色自己视角"的展示逻辑里（当前项目没有做角色扮演视角分离，
   本工单只需确保这是一个新增的、明确标注为"导演视角"的入口即可，不需要做权限系统）。
