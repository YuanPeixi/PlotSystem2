# 工单06：场景结束后动态回写知识图谱

**优先级**：P2
**预估改动范围**：中（新增流程节点，涉及3个文件）
**依赖**：无

---

## 1. 背景

知识图谱（Kuzu）目前**只在 GraphRAG 构建阶段写入一次**，场景模拟运行时产生的新事件、角色关系变化
从不回写图谱。这与 `CLAUDE.md` 中"动态知识图谱"的设计意图不符——图谱在系统运行后是"冻结"的静态快照。

## 2. 精确现状位置

- 写图逻辑现存于：`backend/graphrag_pipeline/pipeline.py` 的 `build_graph` 方法：
  ```python
  async def build_graph(self, entities: list[Entity], relations: list[Relation]) -> None:
      """将实体关系写入 Kuzu 图。"""
      try:
          await self.graph.connect()
      except Exception as exc:
          logger.warning("Kuzu 不可用，跳过图谱写入：%s", exc)
          return
      for e in entities:
          await self.graph.add_entity(e)
      ...
  ```
  这个方法**只在 `GraphRAGPipeline.run()` 中被调用一次**（构建阶段），场景引擎
  `backend/scene_engine/engine.py` 和编排层 `backend/services/orchestrator.py` 的 `run_scene` 函数
  中，全程没有再调用过 `GraphManager` 的任何写入方法（`add_entity`/`add_relation`）。
- 已有的实体抽取器 `backend/graphrag_pipeline/entity_extractor.py` 的 `EntityExtractor.extract(text)`
  方法可以直接复用，输入一段文本、输出 `(entities, relations)`。
- `GraphManager`（`backend/knowledge_graph/graph_manager.py`）的 `add_entity`/`add_relation` 方法
  已经实现了"先查后建"的 upsert 逻辑，可以安全地重复调用而不会产生重复节点。

## 3. 目标（Definition of Done）

1. 在 `backend/services/orchestrator.py` 的 `run_scene` 函数中，场景成功结束（`engine.run()` 返回
   `result` 之后，评估之前或之后均可）新增一步"图谱回写"：
   - 把 `result.dialogue_log` 转换为文本（可复用 `SceneEngine._turn_line` 的思路，或直接把每轮
     `character_name: dialogue` 拼接成一段文本）。
   - 调用 `EntityExtractor().extract(scene_text)` 得到本场新增/提及的实体关系
     （**成本控制**：只在场景级别调用一次，不要逐轮调用，避免 LLM 调用次数暴涨）。
   - 调用 `GraphManager(scene.project_id).add_entity(...)` / `add_relation(...)` 把结果写入图谱。
     注意：新抽取的实体名字可能与已有图谱中的实体是同一个人但 `entity_id` 不同（因为是重新生成的
     UUID），需要先做一次"按名字查找已有实体"的匹配（可以新增一个 `GraphManager.find_entity_by_name(name, entity_type)`
     辅助方法，用 Cypher `MATCH (n:{table} {name: $name}) RETURN n.id` 查询），匹配到就复用已有 ID
     做更新，匹配不到才创建新实体，避免同一角色在图谱里出现重复节点。
2. 额外新增：为本场景创建一个 `Event` 节点（`name` 用场景名，`description` 用场景描述或自动生成的
   `synopsis`），并用 `PARTICIPATED_IN` 关系连接所有参与角色。这是最小成本的"让图谱知道发生过什么"
   方案，即使实体关系抽取暂时不准确，至少事件节点是可靠的。
3. 角色关系变化回写：场景结束后 `CharacterCard.relationships` 可能因场景模拟已经发生变化（目前
   `orchestrator._persist_character_states` 已经会把变化后的 `relationships` 写回角色卡 JSON），
   本工单需要在此基础上**额外**把变化后的 `KNOWS` 关系（`relation_type`/`strength`）同步写入 Kuzu
   图谱（复用 `GraphManager.add_relation`，`source_type`/`target_type` 都传 `"Character"`）。
4. 整个回写过程必须做好异常隔离（参考现有 `build_graph` 的 `try/except` 风格），任何图谱写入失败
   都不应该导致场景运行本身失败或影响 SSE 事件推送。

## 4. 涉及文件

- `backend/services/orchestrator.py`（`run_scene` 函数，新增图谱回写步骤）
- `backend/knowledge_graph/graph_manager.py`（新增 `find_entity_by_name` 辅助方法）
- `backend/graphrag_pipeline/entity_extractor.py`（确认 `extract()` 方法可直接复用，通常不需要改动）

## 5. 验收方式

1. 跑一个全新项目的完整流程：构建 → 规划场景 → 运行场景（对话中提及一个全新的地点或概念）。
2. 场景结束后，通过 `GET /projects/{id}/graph`（前端 GraphViewer 或直接调 API）确认：
   - 新出现的地点/概念节点已经被加入图谱；
   - 场景对应的 `Event` 节点存在，且与参与角色有 `PARTICIPATED_IN` 连接；
   - 如果场景中角色关系发生变化（可以人工在角色卡里预设一个关系强度），确认图谱中 `KNOWS` 关系的
     `strength` 字段已同步更新，而不是停留在构建阶段的初始值。
3. 确认不会产生同名角色的重复节点（用 Cypher `MATCH (n:Character) RETURN n.name` 检查无重复）。
