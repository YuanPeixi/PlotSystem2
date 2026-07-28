# 工单15：角色记忆写入范围错误（只记自己的台词）+ 同一句台词重复入库

**优先级**：P1（影响角色扮演的基本连贯性，且会污染所有 RAG 检索结果）
**预估改动范围**：中（记忆系统 2 个文件 + 场景引擎 1 处写入点 + 测试）
**依赖**：建议在工单14 之后做（复用其快照回填机制）；**应在工单09 之前做**
（09 的分层加权检索建立在"写进去的是什么"之上，写入范围和去重没修之前调权重是徒劳）

---

## 1. 背景

### 1.1 设计意图 vs 实际实现

本项目的记忆隔离借鉴 SillyTavern 的**信息不对等**设计，隔离边界本应是
「**该角色是否在场感知过这件事**」：

- ✅ A 不知道 B 的私生活；
- ✅ B 和 C 私下会面（A 未参与的场景）的内容，A 不知道；
- ✅ **但同一场景里 B 当着 A 的面说的话，A 必须记得**；
- ✅ **A 旁观到的 C 与 D 的争吵，A 也应该记得**（他在场，他听见了）。

实际实现把边界做成了「**这句话是不是我说的**」：

文件：`backend/scene_engine/engine.py`
```python
agent = await self._select_speaker(turn_number, transcript)
raw = await agent.respond(self._scene_context(), transcript)
...
# 实时写入该角色记忆
await agent.memory.add_experience(turn)   # <-- 只有说话者本人被写入
```

文件：`backend/agents/character_agent.py`
```python
async def update_state_after_scene(self, scene_log: list[DialogueTurn]) -> None:
    for turn in scene_log:
        if turn.character_id == self.character_id:   # <-- 同样只挑自己的台词
            await self.memory.add_experience(turn)
    await self.memory.consolidate(force=True)
```

而 `LongTermMemory` 的 collection 是按 `char_{character_id}` 严格隔离的
（`backend/memory/long_term.py`），A 检索不到 B 的库。两者叠加的结果是：

> **每个角色的长期记忆实质上是一份"自言自语的独白集"**——他记得自己说过的每一句话，
> 却完全不记得别人对他说过什么。

场景**内部**因为 prompt 里有完整 transcript 所以看不出问题，一旦跨场景（next_scene /
回滚重演 / 分支）就彻底断档。再叠加工单14 引入的 token 预算裁剪，被裁出窗口的他人台词
**连 RAG 都捞不回来**（因为从来没写进任何一个能被 A 检索到的库）——这是目前唯一会
真正丢失信息的路径。

### 1.2 同一句台词被写入 2~3 次

`backend/memory/memory_manager.py`
```python
async def add_experience(self, turn: DialogueTurn) -> None:
    text = _turn_to_text(turn)
    self.short_term.add(text)
    important = self.episodic.record(turn)
    if important:
        await self.long_term.add(important, {"type": "episodic"})   # 副本 A（立即写）
    if self.short_term.is_full():
        await self.consolidate()                                     # 副本 B（批量写）
```

- **双写**：`SceneEngine.run()` 循环里每轮已经调过一次 `add_experience`，场景结束时
  `update_state_after_scene(new_turns)` 又把自己的台词整体过一遍 `add_experience`
  → 每条台词进两次短期缓冲，固化时向量库里就有两份近似重复的记录。
- **三写**：命中 `_IMPORTANT_KEYWORDS` 的台词还会额外落一条 `type=episodic` 的副本，
  文本与 `type=dialogue` 那条高度重复。

后果：`MEMORY_TOP_K=5` 的检索结果可能被同一句话的 2~3 个副本占满，直接稀释召回质量；
同时 embedding 调用量和存储量成倍上升（工单02 接入远程 embedding 后是**按量计费**的）。

### 1.3 重要程度分析目前形同虚设

`backend/memory/episodic.py` 的 `is_important()` 是 15 个中文关键词的硬编码启发式，
且它**只决定要不要多存一份副本，不参与 `consolidate()` 的过滤**——固化时 20 条缓冲
原封不动全量入库。也就是说系统里存在"重要度判定"，但它没有起到任何筛选作用，
只起到了"制造重复"的作用。

## 2. 目标（Definition of Done）

### 2.1 写入范围改为「在场即记忆」（核心）

1. `SceneEngine` 每产生一个 `DialogueTurn`，对**本场全部参演 agent** 写入记忆，
   而不是只写说话者。
2. **⚠️ 硬约束**：`MemoryManager._turn_to_text()` 会把 `[内心独白]` 一并拼进记忆文本。
   写入**自己**的轮次可以保留内心独白；写入**他人**的轮次**必须剥离 `inner_thought`**，
   否则直接违反 `CLAUDE.md` 第 10 节"禁止泄露其他角色的内心独白"。
   建议实现方式：`add_experience(turn, *, from_self: bool)`，或在 `_turn_to_text` 增加
   `include_inner_thought` 参数，由调用方按 `turn.character_id == self.character_id` 决定。
3. 建议给写入的 metadata 增加 `speaker`（发言者 id/name）与 `self`（bool）字段，
   为工单09 的分层加权、以及未来"我说的话 / 别人对我说的话"差异化召回留出空间。
4. 成本与容量评估（必须在 PR 描述里给出结论）：
   - 写入量从 N 轮变为 **k × N**（k = 参演角色数），embedding 调用量同比例上升；
   - `SHORT_TERM_BUFFER_SIZE=20` 的语义随之改变（原来约等于"我说过的最近 20 句"，
     之后变成"我经历过的最近 20 句"，在 4 人场景里只覆盖 5 轮），需重新评估默认值；
   - 建议 `consolidate()` 改为批量写入（一次 `long_term.add` 多条），减少往返。

### 2.2 消除重复写入

1. **确定唯一写入点**：建议保留 `SceneEngine` 每轮实时写入（SSE/中断场景下更健壮），
   把 `CharacterAgent.update_state_after_scene()` 收敛为只做
   `await self.memory.consolidate(force=True)` 与状态收尾，不再重复 `add_experience`。
2. **episodic 不再存正文副本**：命中重要事件时，不要再 `long_term.add(important, ...)`
   写一条内容重复的记录；改为在该轮记忆固化时把 `metadata["type"]` 标为 `episodic`
   （或增加 `important: True` 标记），保证"同一句台词在向量库里只有一条记录"。
3. 保留 `EpisodicMemory.summary` 作为"最近重要经历"的浓缩文本（工单14 已让它能跨场景
   继承），这部分是摘要而非原文副本，不算重复。

### 2.3 重要程度分析：先去重，再决定要不要升级

本工单**不要求**把关键词启发式换成 LLM 打分。结论建议是：
当前召回质量的瓶颈不在"存了太多不重要的内容"，而在"同一句存了 2~3 份 + 别人的话一句没存"。
先把 2.1/2.2 做完，再由工单09 决定是否需要检索期按 `type`/`self` 加权，
或引入更强的重要度判定。若确实要做，建议做成**检索期加权**而非**写入期丢弃**
（写入期丢弃不可逆，且"当时不重要、后来变关键"的伏笔是本项目的核心叙事资产）。

## 3. 涉及文件

- `backend/scene_engine/engine.py`（每轮写入改为遍历全部参演 agent）
- `backend/agents/character_agent.py`（`update_state_after_scene` 去掉重复写入）
- `backend/memory/memory_manager.py`（`add_experience` 区分自己/他人；`_turn_to_text`
  支持剥离内心独白；`consolidate` 批量写入与 metadata 标记）
- `backend/memory/episodic.py`（不再返回用于额外入库的正文副本，或改由调用方处理）
- `backend/config.py` / `.env.example`（若调整 `SHORT_TERM_BUFFER_SIZE` 默认值）
- `tests/test_scene_engine.py` / `tests/test_character_agent.py`（新增用例）

## 4. 验收方式

1. **跨场景记忆连贯**：连续跑两个场景（同一批角色），在第二场中对角色 A 的记忆做检索，
   确认能召回第一场里 **B 说过的话**（而不是只有 A 自己说过的话）。
2. **内心独白不泄露**：检查 A 的记忆库中所有来自他人轮次的文本，
   确认**不含**这些角色的 `inner_thought` 内容。
3. **不重复入库**：跑完一个场景后统计 A 的长期记忆条数，
   应等于「本场轮次数」而不是其 2~3 倍；同一句台词不应出现多条近似重复记录。
4. **未参与场景不泄露**（回归项）：让 B、C 单独跑一个 A 未参演的场景，
   确认 A 的记忆库中检索不到该场景的任何内容——这是信息不对等的核心保证，不能改坏。
5. `python -m pytest tests/test_scene_engine.py tests/test_character_agent.py -q` 通过。

## 5. 与其他工单的关系

- **工单14（已修复）**：提供了跨场景回填短期缓冲/事件摘要的机制，本工单的写入改动会
  显著提升那套机制的价值（否则继承过来的也只是自言自语）。
- **工单09**：分层加权检索应在本工单之后做，否则是在被重复副本污染的数据上调权重。
- **工单02**：写入量变为 k 倍会同比例增加远程 embedding 调用，需关注配额与成本。
- **工单04/05**：导演视角查看角色记忆时，看到的将是"该角色感知过的全部内容"，
  语义比现在更符合"导演可以看到角色内部状态"的设计。
