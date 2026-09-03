# 工单27：统一上下文压缩管线

**优先级**：P1
**预估改动范围**：小（1 个新文件 + 2 处接入）
**依赖**：无
**分阶段**：27-A（本单，零风险）／27-B（后续，动契约3）

---

## 1. 背景

仓库里已经有 **4 个** 往 LLM prompt 里塞对话记录的地方，各写各的裁剪逻辑，语义还不一致：

| 消费方 | 当前策略 | 问题 |
|---|---|---|
| `CharacterAgent._recent_transcript` | token 预算 + 成块前推（`_transcript_start`） | 正确，但为契约3 特化，别处抄不走 |
| `ScoringSpeakerSelector` | `SELECTOR_TRANSCRIPT_BUDGET` | 每候选一次调用，需要激进裁剪 |
| `DirectorAgent._format_transcript` | **完全没有预算** | `max_turns` 无上限校验，长场景直接爆上下文 |
| `SummaryAgent.generate_output` | `transcript[:12000]` **按字符截尾** | 丢掉的恰好是结局 |

后续工单（18 场记板、20 环境智能体）还会新增消费方。靠"每个人自己记得裁剪"是维持不住的。

## 2. 目的

把"把一堆行装进 token 预算"这件事收敛成**一个纯函数 + 一个可选的 LLM 压缩**，让新增消费方只需要选策略、给预算。

## 3. 目标（Definition of Done）— 27-A

### 3.1 新建 `backend/utils/context.py`

对外暴露：

```python
@dataclass(frozen=True)
class ContextBudget:
    max_tokens: int          # <=0 表示不限
    strategy: str            # "block_drop" | "head_tail" | "tail_only" | "llm_summary"
    head_ratio: float = 0.2
    tail_ratio: float = 0.65

@dataclass
class FitResult:
    text: str
    start_index: int         # block_drop 回写调用方的窗口起点
    dropped: int
    summary: str = ""
    compacted: bool = False

def fit_lines(lines, budget, *, start_hint: int = 0) -> FitResult          # 纯本地，不调 LLM
async def compact_lines(lines, budget, *, model, temperature) -> FitResult  # llm_summary
```

四种策略的语义：

| 策略 | 行为 | 谁用 |
|---|---|---|
| `block_drop` | 从 `start_hint` 起，超预算时**一次性**丢到 75% 水位线并回报新起点 | 角色（保持 prompt 前缀稳定，契约3） |
| `head_tail` | 保开场 + 保结尾，中段替换为 `（此处省略 N 轮）` | 导演评估、总结 |
| `tail_only` | 只保尾部 | selector |
| `llm_summary` | `head_tail` 基础上把中段压成要点摘要 | 导演（continue 累积到超长时） |

### 3.2 三条硬约束

1. **`compact_lines` 的 LLM 调用失败必须降级为 `head_tail`，不得抛出**——契约6，离线/CI 必须能跑通。
2. **中段省略必须带计数**（`（此处省略 N 轮）`），让下游模型知道有缺口，否则会当成连续对话推理。
3. **绝不允许尾部截断**。导演评估与总结最需要的就是结局。

### 3.3 接入两处（本单只动这两处）

- `DirectorAgent`：`_format_transcript` 改为产出行列表，评估处走 `compact_lines`；
- `SummaryAgent.generate_output`：删掉 `transcript[:12000]`。

> ⚠️ `SummaryAgent.generate_synopsis` **不要动**。它是零调用的 dead code（CLAUDE.md §0.3 第 3 条），
> 接线归工单 18。

### 3.4 配置项

```
DIRECTOR_TRANSCRIPT_BUDGET=24000      # 默认足以容纳整场，正常场景触发不到裁剪
DIRECTOR_TRANSCRIPT_STRATEGY=llm_summary   # 超预算时的降级策略
SUMMARY_TRANSCRIPT_BUDGET=40000
```

**必须同步写进 `.env.example`**（CLAUDE.md §0.2：配置项的唯一真值）。

### 3.5 设计取舍说明（写进代码注释，避免后人"优化"回去）

"全文 vs 压缩 vs 滑窗"不做特判：预算设得足够大，20 轮场景（约 3–6K token）根本触发不到裁剪，
即事实上的全文；只有超预算才降级压缩。一套代码覆盖两档，不为常规场景付出额外一次 LLM 调用。

## 4. 涉及文件

- `backend/utils/context.py`（新建）
- `backend/config.py`、`.env.example`
- `backend/agents/director_agent.py`、`backend/agents/summary_agent.py`
- `tests/test_context.py`（新建）
- CLAUDE.md §3 代码地图补一行

## 5. 验收方式

`tests/test_context.py` 覆盖：

1. 空输入不炸、返回空串；
2. **单行就超预算**时不得返回空串（至少保留该行的截断版）；
3. `block_drop` 反复调用时 `start_index` 单调不减（前缀稳定性）；
4. `head_tail` 结果同时包含首行与末行，且含省略计数；
5. `llm_summary` 在 mock `chat_safe` 抛异常时降级为 `head_tail` 且不抛。

## 6. 27-B（后续阶段，本单不做）

把 `CharacterAgent._recent_transcript` 与 `ScoringSpeakerSelector` 迁移到本管线。

⚠️ 角色侧动**契约3**（prompt 前缀稳定 / 成块丢弃不做逐行滑窗）。
验收必须是**行为等价回归**：构造 30 / 60 / 200 行 transcript，断言迁移前后
`_transcript_start` 与输出文本**逐字符相同**。做不到就不要迁。
