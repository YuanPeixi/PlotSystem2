"""CharacterAgent：封装角色智能体。

核心约束（CLAUDE.md 第 10 节）：
- 绝对禁止在 system prompt 中注入 unknown_facts
- 禁止泄露其他角色的内心独白
"""

from __future__ import annotations

from backend.agents.base_agent import autogen_available, make_model_client
from backend.config import settings
from backend.memory import MemoryManager
from backend.models import CharacterCard, DialogueTurn, LoreEntry
from backend.utils.llm import chat_safe
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

【相关记忆】
{memory_context}

【人际关系】
{relationship_summary}

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

    def build_system_prompt(self, scene_context: dict, memory_context: list[str] | None = None) -> str:
        """构建角色 system prompt。严禁注入 unknown_facts。"""
        lore = self._select_lore(scene_context)
        lore_text = "\n".join(f"- {e.content}" for e in lore) or "（你对世界所知有限）"
        known = "\n".join(f"- {f}" for f in self.card.known_facts) or "（无特别已知事实）"
        mem_text = "\n".join(f"- {m}" for m in (memory_context or [])) or "（暂无相关记忆）"

        return _SYSTEM_TEMPLATE.format(
            name=self.card.name,
            persona=self.card.persona or "（待补充）",
            appearance=self.card.appearance or "（未描述）",
            speech_style=self.card.speech_style or "（自然）",
            current_emotion=self.card.current_emotion,
            current_goal=self.card.current_goal or "（顺其自然）",
            current_location=self.card.current_location or scene_context.get("location", "未知"),
            lore_entries=lore_text,
            known_facts=known,
            memory_context=mem_text,
            relationship_summary=self._relationship_summary(),
        )

    # ---- 记忆 ----
    async def retrieve_relevant_memory(self, context: str, top_k: int = 5) -> list[str]:
        chunks = await self.memory.retrieve(context, top_k=top_k)
        return [c.text for c in chunks]

    # ---- 对话生成（直接 LLM 路径，不依赖 AutoGen 可用）----
    async def respond(self, scene_context: dict, transcript: list[str]) -> str:
        """根据场景上下文与已发生对话，生成本角色的下一轮回应原始文本。"""
        query = scene_context.get("description", "") + " " + " ".join(transcript[-4:])
        memory_context = await self.retrieve_relevant_memory(query.strip() or self.name)
        system = self.build_system_prompt(scene_context, memory_context)

        recent = "\n".join(transcript[-12:]) if transcript else "（场景刚刚开始）"
        opening = scene_context.get("opening_narration", "")
        user = (
            f"【场景】{scene_context.get('name', '')} @ {scene_context.get('location', '')}\n"
            f"【场景描述】{scene_context.get('description', '')}\n"
            f"{('【开场】' + opening) if opening else ''}\n\n"
            f"【目前对话】\n{recent}\n\n"
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
    def get_autogen_agent(self, scene_context: dict):
        """返回配置好的 AutoGen AssistantAgent 实例（若可用）。"""
        if not autogen_available():
            raise RuntimeError("autogen-agentchat 未安装。")
        from autogen_agentchat.agents import AssistantAgent

        return AssistantAgent(
            name=self._safe_agent_name(),
            model_client=make_model_client(self.temperature),
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
        """场景结束后固化记忆。"""
        for turn in scene_log:
            if turn.character_id == self.character_id:
                await self.memory.add_experience(turn)
        await self.memory.consolidate(force=True)
