---
paths:
  - "backend/memory/**"
  - "backend/scene_engine/engine.py"
  - "backend/services/inspection.py"
  - "backend/services/orchestrator.py"
  - "backend/snapshot/snapshot_manager.py"
  - "backend/utils/branch_memory.py"
  - "backend/agents/character_agent.py"
  - "backend/config.py"
---

# 记忆层规则（长期记忆 / 固化水位线 / 继承 / Inspection）

本文件管：三层记忆（短期缓冲、事件摘要、Chroma 长期记忆）的写入、固化、寻址、承接与降级，以及角色内部状态的只读查询层。
改 `memory/`、`SceneEngine` 的固化/重放循环、`inspection.py`，或任何构造 `MemoryManager` / 调 `prime` / `consolidate` 的调用方之前读这里。
分支/快照/分叉原语的规则见 `fork-snapshot.md`（陷阱 10 的状态作用域表在那里）。

目录：陷阱 9（水位线）→ 陷阱 11（分支寻址）→ 陷阱 12（老集合承接）→ 契约 4 细则 → 契约 6 细则 → 收尾顺序（原 §6.2）→ Inspection（原 §6.5）。

---

### 陷阱 9 — 记忆固化的水位线

`Scene.turns_consolidated` 是固化水位线，与 `turns_completed` 不同：对话逐轮落盘，固化（`consolidate`）按
`MEMORY_CONSOLIDATE_EVERY_TURNS` 周期发生，场景结束再收尾一次。设计推演与评审过程见
`docs/fix-tickets/26-memory-write-watermark.md#watermark-reasoning`，下面只留不可违反的约束。

1. **续跑要补记忆**：`SceneEngine.run()` 续跑时把 `dialogue_log[turns_consolidated:]` 重新写回各角色记忆，否则"对话恢复了、角色忘了"。
   新增任何写入记忆的路径都必须跟上这条水位线，否则长期记忆重复写入（长期记忆按角色+分支隔离，不随场景状态回滚，重复只会累积）。
2. **水位线与固化写入在同一次落盘内推进**（`run()` 的 `on_persist` 钩子）。等方法返回再存不行：中间隔着后置快照拷贝，
   进程在该窗口被硬杀会整场二次写入。`_consolidate_all` 的推进必须早于 `on_turn`（后者推 SSE，落盘先于推送）。
3. **固化的触发权只在 `SceneEngine`**（工单26）：只有它同时知道"写了几轮"和"水位线该推到哪"。
   `MemoryManager.add_experience` **不得**在缓冲写满时自行 `consolidate()`，那会绕过水位线。
4. **重放循环也要走周期固化**：短期缓冲是定长 deque，一次补回上百轮会静默淘汰最早内容。
5. **周期与缓冲容量是一条真实耦合**：固化跨度达到容量，超出部分在到达 `consolidate()` 前就被挤出，水位线照推，等于**永久且无痕丢失**
   （比重复写入更糟）。两道闸门缺一不可：`Settings` 校验器挡 `周期 >= 容量`（启动即失败）；
   `SceneEngine._buffer_under_pressure` 运行时按缓冲占用率强制固化。后者不可省，因为跨度不只由配置决定
   （`周期=0` 时跨度是整场 `max_turns`；`prime()` 回填使缓冲开跑即非空）。
6. **续跑重放的两个起点不可合并**：长期记忆从 `turns_consolidated` 起补（早于水位线的已入库，重放即重复）；
   **事件摘要（episodic）是独立内存层，与水位线无关**，`[0, turns_consolidated)` 段只走 `replay_episodic`（只补摘要、不写缓冲）。
   按水位线切会让该段的重要事件在续跑后彻底消失。
7. **episodic 重放必须整批、按正文去重**（`EpisodicMemory.replay`）：`prime()` 可能已载入其中一部分
   （正常 continue 命中的 `snapshot_id_after` 全含、崩溃续跑命中的 `snapshot_id_before` 全不含、continue 半途再崩只含前半段）。
   逐轮 append 会让正常 continue 把整段追加第二遍，而 `_events` 只保留末 10 条，更早场次的重要事件被挤出。
   **不要改成"判断快照来源再二分"**：第三种情况两边都不对。
8. **事件摘要条目必须是单行**：它按"一行一条"序列化（`dump`/`load` 以 `\n` 切分），而 `_parse_turn` 的动作正则带
   `re.DOTALL`，跨行 `*动作*` 会产出含换行的 `action`。`_snippet` 统一塌成空格，`load` 把不以 `[重要] ` 开头的续行并回上一条
   （老快照无需迁移）。否则同一条事件"存进去 1 条、读出来 2 条"，去重失效。（`_snippet` 的格式冻结，见陷阱 24。）
   感知同样只有一处：`add_experience` 与 `EpisodicMemory` 用自己的 `character_id` 经 `utils/turns.perceive` 判定，
   **不接受调用方传入的布尔**——现场写入与续跑重放各判一次会漂移，而事件摘要靠两边逐字相同去重（陷阱 24，`scene-engine.md`）。
9. **写入层的幂等兜底**：长期记忆按正文内容寻址（`long_term.memory_id`，sha256），重复写入落在同一 ID 上；先 `get` 判存在再 `upsert`
   （`upsert` 重算 embedding，是远程计费调用）。代价：同一角色说出**完全相同**的一句话会合并成一条。
10. **Chroma 与 SQLite 没有跨库事务**，窗口必然存在，能选的只是往哪边失衡：故意在固化**前**先单独落一次 `dialogue_log`，
    让崩溃后落在"日志有、水位线旧"（续跑重放 → 撞幂等兜底收敛），而不是"角色记得一句日志里没有的台词"。
    **不要把固化挪到 `on_turn` 之后**，那会让 SSE 推送插进固化与落盘之间。

### 陷阱 11 — `branch_id` 是长期记忆寻址的一部分

- `MemoryManager(cid, pid, branch_id)` 的 `branch_id` 留空会退回项目级共享 collection。生产构造点
  （`orchestrator.build_character_agents`、`inspection.inspect_character`）必须传对分支，否则 IF 线会检索到主线的台词。
- `inspection._resolve_branch` 优先级：**显式分支 > 显式快照所属分支 > 场景自身分支 > 默认时点解析出的快照所属分支**。
  场景查询**不得**用继承来的快照反推分支：新分支首场的 `restore_snapshot_id` 指向**来源分支**的快照（契约 4 懒承接），
  按它取集合会把主线分叉后的记忆查回来；该时点之前的记忆已由分叉的 I3 复制进本分支集合。
- **集合名受 Chroma 63 字符上限约束**：`collection = char_{cid hex32}__{sha256(branch_id)[:24]}`（`long_term.branch_suffix`），恰好凑满。
  不要改回拼接原分支 ID（71 字符）：带分支的集合会全部被拒，长期记忆静默降级为内存检索、分叉必然 500，用短 ID 的单测发现不了。

### 陷阱 12 — 改造前的项目级集合靠首次连接一次性承接

`LongTermMemory._connect_sync` 发现无后缀的老集合存在、而本分支集合还没打 `LEGACY_ADOPTED_KEY` 标记时，把老记录 upsert 过来
（原 id、原向量）并打标记；否则老项目升级后角色读到空集合。

- **判据只能是初始化凭据，不能是"集合是否为空"**：空可能是"尚未承接"也可能是"分叉点本来就没记忆"；非空也可能是"承接到一半被杀"。
- 普通升级分支用集合的 `LEGACY_ADOPTED_KEY` 判断可重试承接；从快照创建的分支另有**分支级初始化文件**（`utils/branch_memory.py`），
  表达"快照时该角色尚无 collection"以及"Chroma 缺失、正确起点就是空"。没有后者，角色首次登场会把分叉后的共享记忆灌进历史分支，再次分叉还会继续泄漏。
- 承接失败时**不**打集合标记、**不**删集合，下次连接按原 id 幂等续传；分叉复制或分支级凭据落盘失败则整次 fork 失败，
  不能登记一条看似成功但失忆的分支。

### 契约 4 细则 — 运行时记忆继承链

短期缓冲与事件摘要是纯内存态，必须靠快照续命。`orchestrator._load_inherited_states`（薄封装，真正实现是
`inspection.resolve_scene_states`）的四级优先级，**顺序不可调换**：

1. `snapshot_id_after`（本场跑过 → continue 续跑）
2. `restore_snapshot_id`（回滚重演 / 分叉首场）
3. `snapshot_id_before`（异常恢复）
4. 父场景的 `snapshot_id_after`（next_scene）

取到后经 `MemoryManager.prime()` 回填。`SnapshotManager.restore_snapshot()` 是破坏性的，不是接上记忆的途径（陷阱 10，见 `fork-snapshot.md`）。

### 契约 6 细则 — 优雅降级（记忆相关）

- `kuzu` / `chromadb` / `autogen` 走 try-import + `_XXX_AVAILABLE` 分支，缺失时降级（空图 / 字符重叠伪检索 / 不可用）。离线与 CI 必须能跑通。
- **Chroma 运行期失败只暂时断开**（`LongTermMemory._suspend`）：写入/检索抛错（多半是远程 embedding 抖动）后冷却
  `RETRY_COOLDOWN_SECONDS`，期间写入进暂存区并镜像到降级存储，恢复后先按序补写；每次 `consolidate` 收尾无视冷却再补写一次，
  仍有剩余则打 error。**不要改回"失败一次就 `_collection = None`"**：那会让本场余下写入全部只进内存、场景结束即丢，水位线照推，续跑补不回来。
  暂存区本身是内存态，服务在场景结束前都没恢复时照样丢（已知边界，日志可见）。
- **已知缺陷（2026-11-09 核实）：chromadb 缺失时降级实际不生效**。`memory/embeddings.py` 无条件
  `from chromadb.api.types import ...`，`memory/long_term.py` 又无保护地导入它，所以 chromadb 未安装时 `import backend.memory.long_term`
  就 `ImportError`，`long_term.py` 的 try-import 与 `_CHROMA_AVAILABLE` 分支走不到。真要修需把 embeddings 的 chromadb 导入收进
  try-import（或让 `long_term` 对 embeddings 延迟导入）；按根 CLAUDE.md 第 12 节规则这里**不顺手修**，需要工单。

### 收尾顺序：固化 → 落水位线 → 后置快照（原 §6.2 第 4/5 步）

- 每满 `MEMORY_CONSOLIDATE_EVERY_TURNS` 轮**或缓冲占用逼近容量**，走一次 `_consolidate_all`：先单独落一次日志 → 固化 + 推水位线 + `on_persist`，
  整体早于 SSE 推送。
- 终止后：收尾固化（`consolidate(force=True)`，唯一写入点在周期固化处，不重复写入）→ 同一步内 `on_persist` 把水位线落库 → **再**打后置快照。
  **三者顺序不可颠倒**：先快照再固化，快照里的短期缓冲会带着"即将被固化"的原文，一旦该快照被用于 `prime()`（continue/rollback/next_scene），
  这批台词会在下一次 consolidate 被二次写入；水位线等到 `run()` 返回后才落库，拷快照期间被硬杀就整场二次写入。
- 因此已完成场景的后置快照里 `short_term_buffer` 为空是**正常**的，内容已进长期记忆。
- 崩溃续跑的 `_replay_unconsolidated` 同样按周期固化，且水位线之前那段单独补事件摘要（陷阱 9 第 4、6、7 条）。

### Inspection：角色内部状态的唯一只读查询层（原 §6.5，`services/inspection.py`）

用户面板 / 导演 / 总结智能体共用；读角色内部状态不得另开路径。

- `resolve_scene_states(scene, sm)`：契约 4 四级继承的**唯一实现**；`orchestrator._load_inherited_states` 只是薄封装。
- `load_character_state(...)`：时点解析优先级 `snapshot_id` > `scene_id`（走四级链）> 该角色最近一次出现的快照（可用 `branch_id` 限定）；
  一个快照都没有时退回角色卡当前值，此时来源 id 为空。
- `inspect_character(...)`：叠加角色卡人设与长期记忆检索，产出 `CharacterInspection`。**`include_private=False` 会抹掉 `unknown_facts`，
  结果可能进入角色可见上下文时必须传 False（契约 1）。** 长期记忆**只在显式给出检索词时**才查（避免面板每次打开都触发 embedding 调用）。
- 短期缓冲与事件摘要只存在于快照里；旧接口每次新建 `MemoryManager` 因而恒返回空。
- `CharacterState.long_term_memory_snapshot` 恒为空字符串（陷阱 5，不写）。
