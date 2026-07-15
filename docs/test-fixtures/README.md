# 推演测试夹具

这些夹具用于手工演示、GraphRAG 构建和场景引擎集成测试。每个子目录代表一个独立项目：

- `emotional-rollback/`：强烈情绪转换、关系状态改变，适合测试快照恢复和回滚后的状态同步。
- `long-range-clues/`：跨场景伏笔、延迟揭示和多次线索提及，适合测试长期记忆检索与导演规划。
- `asymmetric-knowledge/`：角色掌握不同事实，适合测试信息不对称和 unknown facts 隔离。

每个目录包含：

- `seed.txt`：非结构化世界观、人物和事件素材，可作为 GraphRAG 的种子文本。
- `direction.txt`：导演推演方向提示词，可在创建场景时作为 narrative goal 或上下文。
- `README.md`：建议的场景顺序、断言点和测试用途。

所有文件均为 UTF-8 编码。目录名和文件名使用 ASCII，便于脚本跨平台发现；文本内容保留中文以覆盖中文分词、编码和 LLM 输入场景。
