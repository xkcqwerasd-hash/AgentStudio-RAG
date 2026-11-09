from typing import Dict, List, Optional
from app.retrieval.vector_store import ScoredChunk

DEFAULT_RAG_SYSTEM_PROMPT = """你是由 AgentStudio-RAG 驱动的企业级智能知识助手。
你的任务是根据下方提供的【参考资料】中经过严格检索确认的事实，准确、严谨、客观地回答用户的问题。

请遵循以下核心原则与证据充分性门禁：
1. 真实与严谨性（严格依据上下文）：你只能且必须完全依据下方提供的【参考资料】回答。严禁凭空捏造事实、虚构文件名或页码。
2. 证据充分性与严格拒答：如果参考资料中没有提供用户提问涉及的核心参数、具体配置数值或事实结论，必须明确声明：“根据当前知识库检索结果，未找到相关依据，无法回答。” 严禁利用通用预训练知识自行脑补内部配置数值、提供业界通用做法或编写推测性顾问指南。
3. 部分可答场景处理：若检索结果仅包含问题涉及的部分信息，请严格仅回答参考资料中已明确记载的部分，并在回答中明确指出缺失部分（例如：“关于[未提及项]，参考资料中未做说明，无法回答”）。
4. 无关技术名词与相关度鉴别：虽然检索结果片段中可能包含某些技术名词，但如果与用户问题所指向的具体机制或配置不直接相关，不能视作充分依据，切勿牵强附会。
5. 来源引用标准：在陈述源自参考资料的观点、数据或规则时，必须在对应句子末尾严格标注引用标记，格式如 [Ref-1]、[Ref-2]。
6. 语言与表达：使用专业、清晰、得体的中文回答，结构层次分明，逻辑客观严谨。
"""


class PromptBuilder:
    def __init__(self, system_prompt: str = DEFAULT_RAG_SYSTEM_PROMPT):
        self.system_prompt = system_prompt

    def build_context(self, chunks: List[ScoredChunk]) -> str:
        if not chunks:
            return "【参考资料】：无相关检索片段。"

        context_blocks = ["【参考资料】："]
        for idx, chunk in enumerate(chunks, start=1):
            src_type = getattr(chunk, "source_type", None)
            if src_type and src_type != "knowledge_base":
                from app.attachments.budget import AttachmentBudgetManager
                prov_str = AttachmentBudgetManager._format_chunk_provenance(chunk)
            else:
                prov_str = f"页码: 第 {chunk.page_number} 页"

            block = (
                f"[Ref-{idx}] 来源文件: {chunk.document_name} | {prov_str} | 相关度: {chunk.similarity_score}\n"
                f"{chunk.content.strip()}"
            )
            context_blocks.append(block)

        return "\n\n".join(context_blocks)

    def build_history_context(self, history_messages: Optional[List[Dict[str, str]]] = None) -> str:
        if not history_messages:
            return ""

        lines = ["【前序对话历史】（仅供理解上下文背景，回答事实依据必须以【参考资料】为准）："]
        for msg in history_messages:
            role = msg.get("role", "")
            content = msg.get("content", "").strip()
            role_label = "用户" if role == "user" else "AI助手"
            lines.append(f"{role_label}: {content}")

        return "\n".join(lines)

    def build_user_prompt(
        self,
        query: str,
        chunks: List[ScoredChunk],
        history_messages: Optional[List[Dict[str, str]]] = None,
        memory_context: Optional[str] = None,
    ) -> str:
        parts = []
        if memory_context:
            parts.append(memory_context)

        context_str = self.build_context(chunks)
        parts.append(context_str)

        history_str = self.build_history_context(history_messages)
        if history_str:
            parts.append(history_str)

        parts.append(f"【当前用户提问】：\n{query}")
        parts.append(
            "请结合以上参考资料作答，并在引用事实依据处标注 [Ref-X] 标记。\n"
            "若参考资料未包含核心依据或缺失具体参数，请明确说明未找到依据并拒绝猜测；若仅含部分依据，仅回答已知部分并说明缺失项。"
        )

        return "\n\n".join(parts)
