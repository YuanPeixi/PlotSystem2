# 工单09：记忆检索质量优化（分层加权 / 中文分词降级）

**优先级**：P3（质量优化项，建议在工单02完成并验证效果后再决定是否投入）
**预估改动范围**：小-中
**依赖**：建议先完成工单02（远程 Embedding），评估效果后再决定本工单投入程度

---

## 1. 背景

即使工单02完成（接入远程中文 Embedding），记忆系统仍有两个可以继续优化的质量问题：
1. 重要事件记忆（episodic）和普通对话记忆（dialogue）混在同一个 ChromaDB collection、
   同权重检索，重要信息可能被大量日常对白"稀释"。
2. 离线降级路径（ChromaDB 完全不可用时的 fallback）目前是最原始的单字符集合交集打分，
   即使工单02完成，这个 fallback 路径依然存在且质量很差，是"最后一道保险"但保险本身不可靠。

## 2. 精确位置

### 2.1 记忆不分层

文件：`backend/memory/memory_manager.py`
```python
async def add_experience(self, turn: DialogueTurn) -> None:
    text = _turn_to_text(turn)
    self.short_term.add(text)
    important = self.episodic.record(turn)
    if important:
        await self.long_term.add(important, {"type": "episodic"})  # metadata 有 type 区分
    if self.short_term.is_full():
        await self.consolidate()

async def consolidate(self, force: bool = False) -> None:
    ...
    for text in items:
        await self.long_term.add(text, {"type": "dialogue"})  # 普通对话也写入同一 collection
    ...
```
写入时确实用 `metadata={"type": ...}` 做了区分，但**检索时**（`long_term.py` 的 `retrieve`/
`_retrieve_sync`）完全没有利用这个 metadata 做过滤或加权：
```python
async def retrieve(self, query: str, top_k: int = 5) -> list[MemoryChunk]:
    if self._collection is not None:
        return await asyncio.to_thread(self._retrieve_sync, query, top_k)
    ...

def _retrieve_sync(self, query: str, top_k: int) -> list[MemoryChunk]:
    res = self._collection.query(query_texts=[query], n_results=top_k)
    # 单次查询，混合返回，不区分 type=episodic 还是 type=dialogue
```

### 2.2 降级检索质量差

文件：`backend/memory/long_term.py`
```python
def _retrieve_fallback(self, query: str, top_k: int) -> list[MemoryChunk]:
    """关键词重叠打分的简单检索。"""
    q_tokens = set(query.lower())          # <-- 中文按单字符拆分，不是分词
    scored: list[MemoryChunk] = []
    for item in self._fallback:
        text = item["text"]
        overlap = len(q_tokens & set(text.lower()))  # <-- 共享字符数打分，噪声很大
        scored.append(MemoryChunk(text=text, score=float(overlap), metadata=item["metadata"]))
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:top_k]
```

## 3. 目标（Definition of Done）

### 3.1 分层加权检索

修改 `LongTermMemory.retrieve` / `_retrieve_sync`，改为两次查询再合并：
```python
def _retrieve_sync(self, query: str, top_k: int) -> list[MemoryChunk]:
    # 优先取重要事件记忆
    episodic_n = max(1, top_k // 2)
    dialogue_n = top_k - episodic_n
    episodic_res = self._collection.query(
        query_texts=[query], n_results=episodic_n, where={"type": "episodic"}
    )
    dialogue_res = self._collection.query(
        query_texts=[query], n_results=dialogue_n, where={"type": "dialogue"}
    )
    # 合并两组结果，episodic 排在前面（更重要）
    ...
```
需要注意：如果某个 collection 里 `episodic` 类型的记录数量不足 `episodic_n`，
ChromaDB 的 `where` 过滤 + `n_results` 组合在结果不足时通常不会报错而是返回不足的数量，
需要写好容错（结果数量可能小于请求数量）。

### 3.2 降级检索改用分词 + TF-IDF/BM25

1. 引入 `jieba` 做中文分词（需要新增依赖 `jieba` 到 `pyproject.toml`，属于新依赖引入，
   注意 CLAUDE.md 第10节规定"未在技术栈列表中的库需在文档更新后才能使用"——本工单需要
   同步在 `CLAUDE.md` 技术栈表格中补充 `jieba` 一行并说明用途）。
2. 将 `_retrieve_fallback` 改为：
   - 对 query 和所有已存文本做 `jieba.lcut()` 分词；
   - 使用简单的 TF-IDF（可以用 `sklearn.feature_extraction.text.TfidfVectorizer`，
     若不想新增 `scikit-learn` 依赖，也可以手写一个轻量 BM25/TF-IDF 打分函数，
     避免过重的依赖引入——**建议先手写轻量版本**，除非项目已经依赖 sklearn）；
   - 按相似度排序返回 top_k。
3. 确保这个改动是**纯离线兜底路径**的优化，不影响工单02完成后的主路径（ChromaDB + 远程
   embedding）行为。

## 4. 涉及文件

- `backend/memory/long_term.py`（`_retrieve_sync` 分层查询逻辑；`_retrieve_fallback` 改用分词打分）
- `pyproject.toml`（新增 `jieba` 依赖）
- `CLAUDE.md`（同步更新技术栈表格，补充 `jieba` 及降级检索算法说明）

## 5. 验收方式

1. 构造一个角色的长期记忆，混合写入若干条 `type=dialogue` 的普通对话和几条
   `type=episodic` 的重要事件文本，调用 `retrieve()` 验证重要事件确实能稳定出现在
   返回结果的前列（即使字面相关性不是最高）。
2. 临时禁用 ChromaDB（模拟不可用场景），验证降级路径下用分词后的查询能够检索到语义相关
   （至少是词汇重叠，而非单字符重叠）的记忆文本，且明显优于旧的字符交集实现
   （可以设计一组对比测试用例，例如查询"背叛"应该比查询"背"更精确匹配到真正相关的记忆）。
