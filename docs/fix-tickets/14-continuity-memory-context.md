# 工单14：场景续跑/回滚后运行时记忆丢失 + 角色对话上下文窗口固定截断

**优先级**：P1（涉及数据丢失与 Roleplay 质量）
**预估改动范围**：中（记忆系统 2-3 个文件 + 场景引擎 1 处）
**依赖**：建议了解工单02（远程 Embedding，长期记忆检索质量是本工单"上下文效率"
问题的另一半解法）、工单09（记忆检索质量优化，方向上互补）

---

## 1. 背景

2026-07-15 讨论中提出"继续按钮没有保留之前的对话"和"角色上下文窗口太死"两个体验问题。
逐项排查代码后，结论是：**对话记录本身（`DialogueTurn`/`dialogue_log`）确实被正确
保留**，但排查中发现了两个更深层的、真实存在的问题，共同导致了"感觉记忆/上下文没有
延续好"的体验：

### 1.1 已确认：对话记录保留机制本身没问题

`backend/services/orchestrator.py` 的 `run_scene()` 在 `scene.dialogue_log` 非空时会
调用 `engine.inject_history(scene.dialogue_log)`，`SceneEngine.run()` 也确实
`turns: list[DialogueTurn] = list(self.scene.dialogue_log)` 保留了已有轮次、
`turn_number` 从历史末尾续接。这部分**不需要修改**。

### 1.2 真正的问题一：`MemoryManager.snapshot()` 存在"先清空再读取"的 bug

文件：`backend/memory/memory_manager.py`

```python
async def snapshot(self, dest_dir: Path | None = None) -> MemorySnapshot:
    """序列化当前记忆状态。dest_dir 给出时导出 ChromaDB 副本。"""
    await self.connect()
    await self.consolidate(force=True)   # <-- 内部会 self.short_term.clear()
    ...
    return MemorySnapshot(
        ...,
        short_term_buffer=self.short_term.dump(),  # <-- 此时永远是空列表！
        episodic_summary=self.episodic.dump(),
    )
```

`consolidate(force=True)` 内部逻辑（同文件）：
```python
async def consolidate(self, force: bool = False) -> None:
    ...
    self.short_term.clear()   # 消费后清空缓冲
    ...
```
`snapshot()` 先强制 consolidate（清空 `short_term`），再读取 `short_term.dump()`，
结果**恒为空**。目前代码库里没有任何调用方使用 `MemoryManager.snapshot()`/`restore()`
（全仓库搜索确认零调用，是死代码），暂未影响生产路径——但如果不修，未来任何人接入
这条路径都会静默得到一个"看起来正常返回、实际上短期记忆总是空"的快照。

**修复**：调整顺序，先 `dump()` 再 `consolidate`，或在 `consolidate` 前保存一份
`short_term.dump()` 的副本用于返回值。

### 1.3 真正的问题二（更重要）：续跑/回滚后，运行时记忆状态从不回填到新的 `MemoryManager`

无论是"继续"（`CONTINUE`）、"下一场"（`NEXT_SCENE`）还是"回滚"（`ROLLBACK`），
`orchestrator.run_scene()` 每次都会调用：

```python
async def build_character_agents(project_id: str, character_ids: list[str]) -> list[CharacterAgent]:
    agents: list[CharacterAgent] = []
    for cid in character_ids:
        card = await repository.get_character(project_id, cid)
        mem = MemoryManager(cid, project_id)   # <-- 每次都是全新实例
        await mem.connect()
        agents.append(CharacterAgent(card, mem))
    return agents
```

`MemoryManager()` 构造时 `self.short_term = ShortTermMemory()`（空）、
`self.episodic = EpisodicMemory(character_id)`（`summary=""`），这两者是**纯内存态**，
不落盘、不从任何地方恢复。而 `SceneEngine._collect_states()` 在场景开始/结束时
确实把 `agent.memory.short_term.dump()` / `agent.memory.episodic.dump()` 正确写入了
快照（`CharacterState.short_term_buffer`/`episodic_summary`），`SnapshotManager`
也确实把这些字段落盘（`backend/snapshot/snapshot_manager.py`）——**但 `rollback` 时
`SnapshotManager.restore_snapshot()` 恢复出的 `CharacterState` 只被
`orchestrator._apply_character_states()` 用来回写 `CharacterCard`（情绪/目标/位置/
关系），`short_term_buffer`/`episodic_summary` 这两个字段在回写路径上被直接丢弃**，
从未被灌回任何新建的 `MemoryManager` 实例。

即：**只有长期记忆（ChromaDB，文件持久化）在续跑/回滚后是连续的，短期缓冲和事件摘要
每次都从零开始**。虽然短期缓冲本身不直接影响 LLM prompt（详见下一节），但事件摘要
（`episodic.summary`）一旦有内容，理论上是角色"最近重要经历"的浓缩记录，目前完全没有
被利用起来延续，是记忆系统三层架构里"看起来实现了、实际没有跨场景生效"的一层。

### 1.4 真正的问题三：角色对话上下文窗口是硬编码的固定截断，与长期记忆检索质量强相关

文件：`backend/agents/character_agent.py`，`respond()` 方法：

```python
async def respond(self, scene_context: dict, transcript: list[str]) -> str:
    query = scene_context.get("description", "") + " " + " ".join(transcript[-4:])
    memory_context = await self.retrieve_relevant_memory(query.strip() or self.name)
    ...
    recent = "\n".join(transcript[-12:]) if transcript else "（场景刚刚开始）"
```

- 用于"检索长期记忆"的查询只用了**最近 4 行**对话拼接，用于"直接展示给 LLM"的近期
  对话固定截断为**最近 12 行**，两个数字都是硬编码常量，不随模型上下文窗口大小、
  场景实际轮次数、单行文本长度变化。场景轮次一旦超过 12 轮（`DEFAULT_MAX_TURNS=20`
  是常见配置，很容易超过），更早的对话内容**完全依赖长期记忆检索**（RAG）才能被
  "记起"，而不是像人类角色扮演那样始终能看到完整最近上下文。
- 这意味着"角色上下文窗口太死"和"记忆检索质量"（工单02/09）实际是同一个体验问题的
  两个成因：截断窗口固定且偏短 + 长期记忆检索质量决定了"被截断出去的内容能不能被
  找回"。工单02（远程 Embedding）已经改善了后者，但前者（截断窗口大小/策略）目前
  没有任何工单覆盖。

## 2. 目标（Definition of Done）

### 2.1 修复 `MemoryManager.snapshot()` 的清空顺序 bug（小改动，顺手做）

- 调整为先 `short_term.dump()` 取值，再执行 consolidate，确保返回值语义正确，
  即使目前是死代码，也要在文档/注释里说明"当前无调用方，属预防性修复"。

### 2.2 续跑/回滚时回填运行时记忆状态

1. `build_character_agents()` 增加可选参数 `character_states: dict[str, CharacterState] | None = None`，
   若传入了对应角色的 `CharacterState`，用其 `short_term_buffer`/`episodic_summary`
   调用新建 `MemoryManager` 的 `short_term.load(...)`/`episodic.load(...)` 完成回填。
2. `orchestrator.run_scene()` 需要能拿到"上一次快照的 `CharacterState`"：
   - `CONTINUE`：可以直接用 `SceneEngine._collect_states()` 同等逻辑在场景暂停/完成时
     已经产出的最新状态（若场景是正常完成后 `continue` 追加轮次，理论上直接复用
     内存里的 `agents` 列表继续跑更彻底，但那需要更大的架构调整——本工单先做
     "从最近一次快照恢复"这个更小的改动：`Scene.snapshot_id_after` 若存在，读取该
     快照的 `character_states` 传给 `build_character_agents`）。
   - `ROLLBACK`：`apply_decision` 里 `restore_snapshot` 已经拿到了
     `restored_states: dict[str, CharacterState]`，需要把它保存下来（例如序列化进
     新创建场景的某个字段，或直接在 `run_scene` 开始时重新调用一次
     `SnapshotManager.restore_snapshot(scene.snapshot_id_before)` 拿到同样的状态）
     传给 `build_character_agents`。
3. 补充测试：验证续跑/回滚后新建的 `CharacterAgent.memory.episodic.summary`
   与快照中记录的一致，而非空字符串。

### 2.3 优化上下文截断策略（可选，视投入决定，建议至少做配置化）

1. 把 `transcript[-4:]`（检索 query 用）和 `transcript[-12:]`（展示给 LLM 用）的
   两个魔法数字提取为 `Settings` 配置项（如 `MEMORY_QUERY_WINDOW`/
   `RECENT_TRANSCRIPT_WINDOW`），至少做到可以不改代码调整。
2. 更进一步（可选，工作量较大）：按 token 数而非行数截断（用 `tiktoken` 或简单的
   字符数估算），让"最近上下文"窗口能自适应不同模型的上下文长度，避免要么截得太
   短浪费模型能力，要么截得太长导致成本上升/超出上下文窗口报错。

## 3. 涉及文件

- `backend/memory/memory_manager.py`（`snapshot()` 顺序修复）
- `backend/services/orchestrator.py`（`build_character_agents` 回填参数、`run_scene`
  获取上一次快照状态）
- `backend/agents/character_agent.py`（`respond()` 截断窗口配置化）
- `backend/config.py`（新增窗口配置项）
- `tests/test_orchestrator.py` / `tests/test_character_agent.py`（对应测试）

## 4. 验收方式

1. 跑一个场景，中途通过快照拿到某角色的 `episodic_summary`（构造一个包含
   `_IMPORTANT_KEYWORDS` 关键词的对话触发事件记录），提交"继续"或"回滚"决策后，
   确认新的 `CharacterAgent.memory.episodic.summary` 与快照记录一致（不是空字符串）。
2. 构造一个超过 12 轮的场景，验证调整窗口配置后（如设为 20），
   `respond()` 传给 LLM 的 `recent` 文本确实包含了此前会被截断掉的更早轮次。
3. `python -m pytest tests/test_character_agent.py tests/test_orchestrator.py -q` 通过。
