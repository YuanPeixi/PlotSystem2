---
paths:
  - "backend/graphrag_pipeline/**"
  - "scripts/reclassify_lore.py"
  - "scripts/extract_objects.py"
  - "backend/agents/character_agent.py"
---

# 构建流水线与世界设定可见性（原 §6.1 / 陷阱 23 / 契约 1 细则 / §10.3 脚本）

本文件管构建期（`run_graphrag`）、世界设定（`LoreEntry`）的知情范围分发、构建期物件抽取。
**小节目录**：构建流程 · 陷阱 23（LoreEntry.scope） · 契约 1 细则（设定分发） · 构建期物件抽取 · 修复脚本

---

## 构建流程（原 §6.1）

后台任务（`orchestrator.run_graphrag`）。进度经 `_set_build_status` 同时写内存与 `build_status.json`；失败时 stage 写成 `失败: xxx`，项目状态退回 `initializing`。
角色卡在生成过程中通过 `on_character` 回调**逐个落盘**，前端轮询即可增量预览。
顺序（不可调换）：实体抽取 → 角色卡 → 世界规则抽取 → **设定可见性判定** → 物件抽取与可见性判定。可见性判定要用角色卡的已知/未知事实作证据，所以必须排在角色卡之后；物件同理（别名要按角色名过滤）。
世界设定由 `run_graphrag` 按 `lore_for_character` 分发进各角色卡；判定失败的条目不发给任何角色，条数记在构建状态的 `lore_withheld`（工单29）。
物件是附加层：抽取失败只 warning，不让整个构建失败。

---

## 陷阱 23 — `LoreEntry.scope` 只认两种取值，失败一律收紧（工单29）

- `scope` 只有 `global`（全员可见）与 `character:{角色 id}`（仅本人）。读取侧 `CharacterAgent._select_lore` 只注入 `global` 与**完全等于**本角色的那一种，其余（按角色名写的、拼错的）一律丢弃；旧实现用 `endswith`，`character:x-{id}` 也能混进来。
- 写入侧分类器拿不准的（调用失败、漏判、取值非法、知情者名字对不上任何角色）都不发给任何角色，**绝不退回 global**：漏一条公开设定只是少点背景，泄露一条秘密则信息差无痕消失，且角色卡是拷贝、不会自愈。
- **分类器自身的证据也可能不全，两道安全网缺一不可**（固定阈值总会被超过，调大阈值不是修复；推演与评审经过见 `docs/fix-tickets/29-lore-visibility.md#evidence-truncation`）：
  1. `_describe_characters` 裁剪每个角色的 `unknown_facts`（条数/单条字数），裁掉的恰好可能是"证明某角色不知道"的反证。分类器既可能把本该私有的设定判成 `public`，也可能把本不该知情的角色错判进某条 `private` 的 `known_by`。`classify()` 经 `_unknown_facts_truncated` 检测到任一角色被裁，本轮全部 public / private 判定一律收紧为不发（`hidden` 本来就不发给任何人，不受影响；`known_facts` 被裁只会让知情者名单偏少，方向安全，不连带收紧）；
  2. **送进分类 prompt 的设定正文同样有裁剪**（`_ENTRY_CONTENT_CHARS`）：秘密若藏在裁剪点之后，分类器可能依据看得到的公开前半段判为 public / private，而**分发用的是完整正文**。每条设定的正文裁剪独立判定，只影响这一条；
  两者都是结构性兜底，不是靠调大 `_MAX_FACTS` / `_FACT_CHARS` 顶替（同世界变量预算"两道闸门缺一不可"的教训）。
- 多人知情的私有设定按知情者各发一份副本，`LoreEntry` 不加"知情者列表"字段。
- 与陷阱 20 / 契约 1 的关系：设定进角色 system prompt，所以分发范围就是信息不对称的一部分。

---

## 契约 1 细则 — 世界设定按知情范围分发

- 公开的进全部角色卡，私有的只进知情者（`character:{id}`），隐藏的与判定失败的不进任何角色卡。
- 种子里分属不同角色的秘密若以 `global` 发下去，等于构建那一刻就向全员泄密。
- `unknown_facts` 允许出现的位置之一就是构建期的 `LoreVisibilityClassifier`（以及 `PersonaBuilder` 生成过程与面向导演/用户的 API），**不得**扩散到角色可见上下文（契约 1 其余部分见根文档）。

---

## 构建期物件抽取（工单24，`world_rules.py::ObjectExtractor` + `pipeline.py::build_objects`）

- 流程：抽取（只抽内容）→ 复用设定分类器判定可见性 → `clamp_object` 压回预算。构建与 `scripts/extract_objects.py` 共用 `build_objects` 这一份。
- 与角色同名的物件丢弃并 warning（会让动作意图预过滤每轮命中）；分块重叠或多段文本提到同一件东西按名称合并别名与规则，公开描述留第一份。
- 送进可见性分类的只有名称与公开描述：隐藏规则按定义谁都不发，拿去分类只会让模型因规则里的秘密把整个物件判成 hidden。继承设定分类器的全部收紧安全网（陷阱 23）；拿不准一律 `hidden`（`object_visibility`）。
- 预算：构建期这道闸门截断并 warning（没人能当场改），用户编辑那道才是 422。
- 物件的隐藏规则只进导演与环境层（陷阱 27 / `world-environment.md`），不进角色 prompt。

---

## 修复与补抽脚本（原 §10.3）

- 世界设定可见性修复：`python -m scripts.reclassify_lore --project ID [--apply]`。工单29 之前构建的项目都需要跑一次；会调 LLM，预览与写入是两次独立判定；默认预览，`--apply` 先备份再写；任一批判定调用失败就整个不写，让人重试。
- 已有项目补抽物件：`python -m scripts.extract_objects --project ID [--apply]`。只新增（名称或别名撞上已有物件的跳过，不覆盖用户手改），任一段抽取或判定失败整个不写。
