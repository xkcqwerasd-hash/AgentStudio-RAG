from typing import Dict, List, Optional
from app.retrieval.vector_store import ScoredChunk

DEFAULT_RAG_SYSTEM_PROMPT = """你是由 AgentStudio-RAG 驱动的企业级智能知识助手。
你的任务是根据下方提供的【参考资料】中经过严格检索确认的事实，准确、严谨、客观地回答用户的问题。

请遵循以下核心原则：
1. 真实与严谨性：你只能依据提供的参考资料进行回答。绝不允许凭空捏造事实、推测未提及的数据、或编造虚假的文件名与页码。
2. 来源引用标准：在陈述源自参考资料的观点或数据时，必须在对应句子末尾严格标注引用标记，格式如 [Ref-1]、[Ref-2]。
3. 证据不足时的声明：如果参考资料中没有包含回答用户问题所需的充分事实，请直接明确告知：“根据当前知识库检索结果，未找到与您提问相关的充分事实证据，无法给出确切回答。” 切勿强行编造猜测。
4. 语言要求：使用专业、清晰、得体的中文回答，结构层次分明。
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
        parts.append("请结合以上参考资料作答，并在引用事实依据处标注 [Ref-X] 标记：")

        return "\n\n".join(parts)
