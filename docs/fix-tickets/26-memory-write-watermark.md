# 工单26：记忆写入点与固化水位线统一

**优先级**：P2
**预估改动范围**：中（记忆子系统 + 引擎固化时机 + 一次历史数据评估）
**依赖**：无（PR #15 已修掉同族的"水位线落盘时机"缺陷，本单处理剩余两项）
**契约影响**：契约 6（降级路径不得被破坏）

---

## 1. 背景

`Scene.turns_consolidated` 是"哪些轮次已经进过长期记忆"的水位线（CLAUDE.md §4.2 陷阱 9）。
崩溃续跑时 `SceneEngine.run()` 靠它决定重放哪一段：

```python
for past in turns[self.scene.turns_consolidated :]:
    await self._remember(past)
```

这个机制本身成立，但它**只覆盖了场景正常结束时的那一次固化**。仓库里实际存在两处
"绕过水位线"的写入，其中一处已在 PR #15 修掉，另一处仍在。

| | 缺陷 | 状态 |
|---|---|---|
| A | 水位线赋值后要等 `run()` 返回才落库，中间隔着拷贝几十兆 kuzu/chroma 的后置快照；进程被硬杀就整场二次写入 | ✅ 已修（`run()` 的 `on_persist` 钩子） |
| B | **场景内自动固化完全不推进水位线** | ❌ 本工单 |

## 2. 缺陷 B 的精确位置

`backend/memory/memory_manager.py` 的 `add_experience` 结尾：

```python
    self.short_term.add(text, important=important, is_self=from_self, speaker=turn.character_name)
    if self.short_term.is_full():
        await self.consolidate()      # <-- 写进了长期记忆，但 Scene.turns_consolidated 毫不知情
```

- `SHORT_TERM_BUFFER_SIZE` 默认 40；工单15 的"在场感知"让**每个角色每轮各记一条**，
  所以单次 `run()` 跑到第 40 轮时，前 40 轮就已经落进长期记忆。
- 此时 `turns_consolidated` 仍是进入本次 `run()` 时的旧值（通常 0）。
- 第 45 轮崩溃 → 续跑重放 `turns[0:45]` → **前 40 轮第二次入库**。

**触发条件**：单次 `run()` 产生 ≥ `SHORT_TERM_BUFFER_SIZE` 轮，且随后异常中断。
默认 `DEFAULT_MAX_TURNS = 20` 碰不到，但 `SceneConfig.max_turns` 由
`DirectorAgent.plan_scene` 输出或 `CreateSceneRequest` 指定，**没有上限校验**，因此可达。

> `continue` 续跑不受影响：进来时 `turns_consolidated == len(turns)`，重放循环为空，
> 且每次 `run()` 的缓冲从空开始，单次只加约 6 轮。

## 3. 为什么必须修（而不是"概率低，先放着"）

长期记忆按**角色+项目**共享，不随分支回滚（CLAUDE.md §4.2 陷阱 10）：

- 重复只会累积，不会自愈，也没有任何现存路径能清理；
- 检索是 `top_k`，同一句台词占掉两个坑，**污染的是检索质量，不只是磁盘**；
- 全程静默，没有任何日志会提示"这条写过了"。

## 4. 目标（Definition of Done）

### 4.1 主线：把固化时机收归引擎

让"写入长期记忆"和"推进水位线"变成原子的一对。

1. `MemoryManager.add_experience` **移除**结尾的自动 `consolidate()`；
   `consolidate()` 保留（`force=True` 由引擎显式调用）。
2. `SceneEngine.run()` 的对话循环里，每 `settings.CONSOLIDATE_EVERY_N_TURNS`
   轮（新增配置项，默认取 `SHORT_TERM_BUFFER_SIZE` 的一半，如 20）做一次显式固化：

   ```python
   if turn_number - self.scene.turns_consolidated >= settings.CONSOLIDATE_EVERY_N_TURNS:
       for agent in self.agents:
           await agent.memory.consolidate(force=True)
       self.scene.turns_consolidated = turn_number
       if on_persist:
           await on_persist()          # 与缺陷 A 的修法同一条路径
   ```

3. 场景结束时的那次固化（步骤 4）保持不变。

> ⚠️ **不要把水位线推进放在 `on_turn` 之后**。`on_turn` 会推 SSE，
> 而落盘必须先于推送（工单23 的既有约定）。用 `on_persist` 这条不推事件的路径。

**为什么不是"让 `add_experience` 回报固化条数给引擎"**：记忆按**条**计数（每轮 × 每个
在场角色），引擎按**轮**计数，两者不是 1:1；而且各角色的缓冲在 `prime()` 回填后可能
深浅不一，会各自在不同轮次触发。把触发权收归引擎才能保证全场同步。

### 4.2 兜底：长期记忆写入改为确定性 ID + upsert

即使 4.1 做完，未来每新增一个写入点（世界变量写回、环境智能体裁决落档、动态图谱写回）
都得记得维护水位线。加一层幂等兜底：

`backend/memory/long_term.py` 目前是

```python
self._collection.add(documents=[text], metadatas=[safe_meta], ids=[new_id()])
```

改为确定性 ID + `upsert`：

```python
doc_id = hashlib.sha1(f"{self.character_id}|{text}".encode()).hexdigest()
self._collection.upsert(documents=[text], metadatas=[safe_meta], ids=[doc_id])
```

**必须一并确认的三点**：
1. 降级路径（`self._collection is None` 时的 `self._fallback` 列表）也要同样去重，
   否则契约 6 的两条路径行为不一致；
2. `upsert` 在当前 chromadb 版本上可用（不可用则退回"先 `get(ids=...)` 再决定
   `add`"，但要注意这会多一次往返）；
3. 确认 embedding 调用是否会因 `upsert` 重复触发 —— 若会，加一次 `get` 前置判断更省钱。

**已知副作用（需明确接受）**：同一角色在不同场次说出**完全相同**的一句话会被合并成一条。
工单15 已经做过"同句去重"，方向一致；但要在 `long_term.py` 里写一行注释说明这是有意为之。

### 4.3 历史数据：只评估，不自动清理

现网 `data/projects/*/chroma_db/` 里可能已存在重复条目。

**本工单不提供自动清理**（删向量库数据不可逆，且无法区分"重复写入"与"角色确实说了两遍"）。
只要求：
- 提供一个只读脚本 `scripts/check_memory_dupes.py`，按 `char_*` 集合统计完全相同文本的
  出现次数，输出 Top N，供人工判断是否需要重建；
- 在脚本注释里写明"确认重复严重时，最干净的处理是删掉 `chroma_db/` 重跑"
  （长期记忆可从 `dialogue_log` 重建，不是唯一真相源）。

## 5. 涉及文件

- `backend/memory/memory_manager.py`（移除自动 consolidate）
- `backend/memory/long_term.py`（确定性 ID + upsert + 降级路径去重）
- `backend/scene_engine/engine.py`（周期性显式固化 + 推进水位线 + `on_persist`）
- `backend/config.py`（新增 `CONSOLIDATE_EVERY_N_TURNS`）
- `.env.example`（配置项唯一真值，必须同步）
- `scripts/check_memory_dupes.py`（新增，只读）
- `CLAUDE.md` §4.2 陷阱 9、§6.2 第 5 步、§12.1（修完删掉"场景内自动固化绕过水位线"一行）

## 6. 验收方式

**自动化**（`tests/test_scene_engine.py` / `tests/test_character_agent.py`）：

| 用例 | 断言 |
|------|------|
| `test_periodic_consolidation_advances_watermark` | 跑一场 `max_turns` 大于 `CONSOLIDATE_EVERY_N_TURNS` 的场景，断言中途 `on_persist` 被调用且每次调用时 `turns_consolidated == 当时的 turn_number` |
| `test_resume_after_mid_scene_consolidation_no_duplicate` | 构造"已中途固化 + 崩溃"的场景（`turns_consolidated` 设为中途值），续跑后断言长期记忆里没有重复文本 |
| `test_long_term_add_is_idempotent` | 同一 `character_id` 连写两次相同文本，检索结果只有一条 |
| `test_long_term_fallback_is_idempotent` | 同上，但在 `_CHROMA_AVAILABLE = False` 的降级路径上（契约 6） |
| 既有 `test_resume_after_crash_replays_unconsolidated_turns_into_memory` | 必须继续通过 —— 4.1 不能把"该补的补不回来"一起改没了 |

> **测试陷阱**：`test_scene_engine.py` 里多处 `patch(... MemoryManager.consolidate ...)`。
> 4.1 改了触发时机后，这些 patch 的语义会变，逐个确认断言是否仍成立，
> **不要图省事直接删断言**。

**手工**：把 `max_turns` 调到 50 跑一场，中途 `Ctrl+C` 杀掉进程 → 重启 → 续跑 →
用 `scripts/check_memory_dupes.py` 确认没有新增重复。

## 7. 明确不做

| 项 | 理由 |
|----|------|
| 自动清理历史重复数据 | 不可逆，且无法区分真重复与真复述；只给只读检查脚本 |
| 给长期记忆加分支维度 | 归工单 08 阶段 A，与本单正交 |
| 改 `SHORT_TERM_BUFFER_SIZE` 的默认值 | 缓冲容量与固化周期是两件事，4.1 之后缓冲只负责"最近上下文" |
| 给 `max_turns` 加上限校验 | 可以顺手加，但不是本单的必要条件；4.1 之后超长场景本身已安全 |
