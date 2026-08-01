"""CharacterAgent：封装角色智能体。

核心约束（CLAUDE.md 第 10 节）：
- 绝对禁止在 system prompt 中注入 unknown_facts
- 禁止泄露其他角色的内心独白

Prompt 结构约定（工单14）：
system 消息只放**整场不变**的内容（人设/世界观/已知事实/关系/场景设定/格式规范），
每轮变化的内容（对话进展、检索到的记忆、发言指令）一律放到 user 消息里，且
"目前对话"只在末尾追加、不做逐行滑窗。这样同一角色在同一场景内的 prompt 前缀
保持稳定，可以命中服务端 prefix cache（显著降低延迟与成本）。
"""

from __future__ import annotations

from backend.agents.base_agent import autogen_available, make_model_client
from backend.config import settings
from backend.memory import MemoryManager
from backend.models import CharacterCard, DialogueTurn, LoreEntry
from backend.utils.llm import chat_safe, estimate_tokens
from backend.utils.logger import get_logger

logger = get_logger("agents.character")

_SYSTEM_TEMPLATE = """你是【{name}】。

【角色设定】
{persona}

【外貌】
{appearance}

【说话风格】
{speech_style}

【当前状态】
- 情绪：{current_emotion}
- 目标：{current_goal}
- 位置：{current_location}

【你所了解的世界】
{lore_entries}

【你知道的事实】
{known_facts}

【人际关系】
{relationship_summary}

【当前场景】
{scene_brief}

【行为格式规范】
- 对白直接说出，无需引号
- 动作用 *星号包裹*，如：*走向窗边*
- 内心独白用 [方括号包裹]，如：[他在说谎]
- 每轮回应必须包含至少一种格式
- 保持角色一致性，不得跳出角色视角
- 你只知道你"已知"的信息，不得使用你不该知道的信息
- 回应要简洁有戏剧张力，控制在 3 句话以内
"""


class CharacterAgent:
    """角色智能体。每个实例持有独立的 MemoryManager。"""

    def __init__(
        self,
        character_card: CharacterCard,
        memory_manager: MemoryManager,
        temperature: float = 0.8,
    ):
        self.card = character_card
        self.memory = memory_manager
        self.temperature = temperature
        self.model = settings.character_model
        # "目前对话"窗口的起点。仅在超出 token 预算时**成块前推**，
        # 而非每轮滑动一行，从而让相邻若干轮的 prompt 前缀保持一致。
        self._transcript_start = 0

    @property
    def character_id(self) -> str:
        return self.card.character_id

    @property
    def name(self) -> str:
        return self.card.name

    # ---- Prompt 构建 ----
    def _select_lore(self, scene_context: dict) -> list[LoreEntry]:
        """根据场景上下文关键词筛选相关 LoreEntry。"""
        ctx_text = " ".join(str(v) for v in scene_context.values())
        relevant: list[tuple[int, LoreEntry]] = []
        for entry in self.card.world_lore_entries:
            # global 始终注入；character 范围仅匹配本角色
            if entry.scope.startswith("character:") and not entry.scope.endswith(self.character_id):
                continue
            hit = any(kw and kw in ctx_text for kw in entry.keywords)
            score = entry.priority + (5 if hit else 0)
            relevant.append((score, entry))
        relevant.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in relevant[:6]]

    def _relationship_summary(self) -> str:
        if not self.card.relationships:
            return "（暂无明确关系）"
        lines = []
        for state in self.card.relationships.values():
            lines.append(
                f"- 对 {state.target_character_id}：{state.relation_type}"
                f"（亲密度 {state.strength:+.1f}）{state.notes}"
            )
        return "\n".join(lines)

    @staticmethod
    def _scene_brief(scene_context: dict) -> str:
        """场景静态简介。整场不变，放进 system 以稳定 prompt 前缀。"""
        lines = []
        name = scene_context.get("name", "")
        location = scene_context.get("location", "")
        if name or location:
            lines.append(f"{name} @ {location}".strip(" @"))
        if scene_context.get("description"):
            lines.append(str(scene_context["description"]))
        if scene_context.get("opening_narration"):
            lines.append(f"开场：{scene_context['opening_narration']}")
        return "\n".join(lines) or "（场景信息待补充）"

    def build_system_prompt(self, scene_context: dict, memory_context: list[str] | None = None) -> str:
        """构建角色 system prompt。严禁注入 unknown_facts。

        memory_context 默认不注入：检索结果每轮都变，放进 system 会让
        prompt 前缀每轮失效。respond() 会改为把记忆放进 user 消息。
        仅 AutoGen 路径（system_message 是唯一注入点）才需要传入。
        """
        lore = self._select_lore(scene_context)
        lore_text = "\n".join(f"- {e.content}" for e in lore) or "（你对世界所知有限）"
        known = "\n".join(f"- {f}" for f in self.card.known_facts) or "（无特别已知事实）"

        prompt = _SYSTEM_TEMPLATE.format(
            name=self.card.name,
            persona=self.card.persona or "（待补充）",
            appearance=self.card.appearance or "（未描述）",
            speech_style=self.card.speech_style or "（自然）",
            current_emotion=self.card.current_emotion,
            current_goal=self.card.current_goal or "（顺其自然）",
            current_location=self.card.current_location or scene_context.get("location", "未知"),
            lore_entries=lore_text,
            known_facts=known,
            relationship_summary=self._relationship_summary(),
            scene_brief=self._scene_brief(scene_context),
        )
        if memory_context:
            prompt += "\n【相关记忆】\n" + "\n".join(f"- {m}" for m in memory_context)
        return prompt

    # ---- 记忆 ----
    async def retrieve_relevant_memory(self, context: str, top_k: int = 5) -> list[str]:
        chunks = await self.memory.retrieve(context, top_k=top_k)
        return [c.text for c in chunks]

    # ---- 上下文窗口 ----
    def _recent_transcript(self, transcript: list[str]) -> str:
        """构建"目前对话"文本。

        与旧实现（固定 `transcript[-12:]` 滑窗）的两点区别：
        1. 默认不按行数截断，只受 token 预算约束——一整场 20 轮对话通常完整保留，
           不再让 12 轮以外的内容只能靠 RAG 找回；
        2. 超预算时**一次性成块丢弃**最早内容至水位线，并把起点记在实例上，
           之后若干轮沿用同一起点，保证 prompt 前缀稳定、可命中 prefix cache。
        """
        if not transcript:
            return "（场景刚刚开始）"

        start = self._transcript_start
        window = settings.RECENT_TRANSCRIPT_WINDOW
        if window > 0:
            start = max(start, len(transcript) - window)

        budget = settings.TRANSCRIPT_TOKEN_BUDGET
        if budget > 0:
            total = sum(estimate_tokens(line) for line in transcript[start:])
            if total > budget:
                target = int(budget * 0.75)  # 丢到水位线，留出后续若干轮的增长空间
                while start < len(transcript) - 1 and total > target:
                    total -= estimate_tokens(transcript[start])
                    start += 1
                logger.debug(
                    "角色 %s 对话上下文超预算，窗口起点前推至第 %d 行", self.name, start
                )

        self._transcript_start = start
        body = "\n".join(transcript[start:])
        if start > 0:
            body = f"（更早的 {start} 轮已省略，其要点见下方【你此刻想起的】）\n{body}"
        return body

    # ---- 对话生成（直接 LLM 路径，不依赖 AutoGen 可用）----
    async def respond(self, scene_context: dict, transcript: list[str]) -> str:
        """根据场景上下文与已发生对话，生成本角色的下一轮回应原始文本。"""
        q_window = max(settings.MEMORY_QUERY_WINDOW, 0)
        tail = transcript[-q_window:] if q_window else []
        query = (scene_context.get("description", "") + " " + " ".join(tail)).strip()
        memory_context = await self.retrieve_relevant_memory(
            query or self.name, top_k=settings.MEMORY_TOP_K
        )

        # 静态部分进 system（整场不变），动态部分进 user 且按"历史在前、变化在后"排列，
        # 使得 prompt 前缀随轮次只增不改，命中服务端 prefix cache。
        system = self.build_system_prompt(scene_context)
        recent = self._recent_transcript(transcript)
        mem_text = "\n".join(f"- {m}" for m in memory_context) or "（暂无相关记忆）"
        user = (
            f"【目前对话】\n{recent}\n\n"
            f"【你此刻想起的】\n{mem_text}\n\n"
            f"现在轮到你（{self.name}）发言，请按行为格式规范回应。"
        )
        return await chat_safe(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self.temperature,
            model=self.model,
        )

    # ---- AutoGen 集成 ----
    # ⚠️ 未接入实际场景流程：SceneEngine（backend/scene_engine/engine.py）目前调用的是
    # respond()（直连 chat_safe），不会调用本方法。这里保留是为未来可能的
    # AutoGen GroupChat + 工具调用式环境交互预留（见 docs/fix-tickets/11-selector-and-world-interaction.md）。
    # 后来者接入前请重新验证这条路径，不要假设它已经过端到端测试。
    def get_autogen_agent(self, scene_context: dict):
        """返回配置好的 AutoGen AssistantAgent 实例（若可用）。"""
        if not autogen_available():
            raise RuntimeError("autogen-agentchat 未安装。")
        from autogen_agentchat.agents import AssistantAgent

        return AssistantAgent(
            name=self._safe_agent_name(),
            model_client=make_model_client(self.temperature, model=self.model),
            system_message=self.build_system_prompt(scene_context),
        )

    def _safe_agent_name(self) -> str:
        """AutoGen agent 名要求为 ASCII 合法标识符（不含中文/空格）。"""
        import re

        base = re.sub(r"[^A-Za-z0-9]+", "_", self.name).strip("_")
        if not base or not base[0].isalpha():
            base = f"char_{base}" if base else "char"
        return f"{base}_{self.character_id[:8].replace('-', '')}"

    # ---- 状态更新 ----
    async def update_state_after_scene(self, scene_log: list[DialogueTurn]) -> None:
        """场景结束后固化记忆。

        逐轮写入已在 SceneEngine.run() 循环里对全部参演角色实时完成（工单15），
        这里只做批量固化，不再重复 add_experience，避免同一句台词入库两次。
        """
        await self.memory.consolidate(force=True)
