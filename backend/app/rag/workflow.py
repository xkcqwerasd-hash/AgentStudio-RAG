import asyncio
from datetime import datetime, timezone
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.attachments import attachment_retrieval_service
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import (
    ConversationNotFoundError,
    KnowledgeBaseNotFoundError,
    VectorModelMismatchError,
)
from app.core.logging import logger
from app.llm.base import BaseLLMClient
from app.llm.embeddings import get_embedding_client
from app.llm.openai_client import get_llm_client
from app.memory import prepare_memory_context, run_memory_extraction_task
from app.models.chunk import DocumentChunk
from app.models.conversation import Conversation, ConversationAttachment, Message
from app.models.knowledge import KnowledgeBase
from app.rag.citation import CitationExtractor
from app.rag.context_budget import ContextBudgetManager
from app.rag.prompt_builder import PromptBuilder
from app.rag.query_rewriter import QueryRewriter
from app.retrieval.reranker import CrossEncoderReranker, RerankResult
from app.retrieval.rrf_fusion import HybridRetrievalResult, HybridRetriever
from app.retrieval.vector_store import VectorRetriever
from app.routing.schemas import RouteDecision
from app.schemas.chat import ChatRequest, ChatResponse, ExecutionInfo


class WorkflowRAGEngine:
    def __init__(
        self,
        db: AsyncSession,
        llm_client: Optional[BaseLLMClient] = None,
        llm_model: Optional[str] = None,
    ):
        self.db = db
        self.hybrid_retriever = HybridRetriever(db)
        self.retriever = self.hybrid_retriever.vector_retriever
        self.reranker = CrossEncoderReranker()
        self.prompt_builder = PromptBuilder()
        self.embedding_client = get_embedding_client()
        self.llm_client = llm_client or get_llm_client()
        self.llm_model_name = llm_model or getattr(self.llm_client, "model", None) or settings.LLM_MODEL
        self.query_rewriter = QueryRewriter(self.llm_client)
        self.budget_manager = ContextBudgetManager()

    async def execute(
        self,
        req: ChatRequest,
        decision: Optional[RouteDecision] = None,
        user_id: Optional[str] = None,
    ) -> ChatResponse:
        start_time = time.perf_counter()

        # 0. Early query validation against max query token budget (M2.4)
        self.budget_manager.validate_user_query(req.query)

        # 1. Handle conversation creation, validation, and multi-turn history loading
        eff_user_id = user_id or settings.DEFAULT_LOCAL_USER_ID
        history_messages: List[Dict[str, str]] = []
        conv_id = req.conversation_id
        if not conv_id:
            conv = Conversation(
                owner_id=eff_user_id,
                knowledge_base_id=req.knowledge_base_id or None,
                title=req.query[:30] if len(req.query) > 30 else req.query,
                mode=req.mode,
            )
            self.db.add(conv)
            await self.db.flush()
            conv_id = conv.id
        else:
            conv_stmt = select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.owner_id == eff_user_id,
            )
            conv_res = await self.db.execute(conv_stmt)
            conv = conv_res.scalar_one_or_none()
            if not conv:
                raise ConversationNotFoundError(conv_id)

            if req.knowledge_base_id and not conv.knowledge_base_id:
                conv.knowledge_base_id = req.knowledge_base_id

            # Load recent historical messages (ordered chronologically)
            history_stmt = (
                select(Message)
                .where(Message.conversation_id == conv.id)
                .order_by(Message.created_at.desc())
                .limit(settings.MAX_HISTORY_TURNS * 2)
            )
            history_res = await self.db.execute(history_stmt)
            raw_history = list(reversed(history_res.scalars().all()))

            # Enforce max history character budget
            total_chars = sum(len(m.content) for m in raw_history)
            while raw_history and total_chars > settings.MAX_HISTORY_CHARS:
                dropped = raw_history.pop(0)
                total_chars -= len(dropped.content)

            for m in raw_history:
                if m.role in ("user", "assistant"):
                    cleaned = (
                        m.content.replace("\n\n[回答已被用户中止]", "")
                        .replace("[回答已被用户中止]", "")
                        .strip()
                    )
                    if cleaned:
                        history_messages.append({"role": m.role, "content": cleaned})

        # Load completed attachments for current conversation
        conversation_attachments: List[ConversationAttachment] = []
        if conv_id:
            att_stmt = (
                select(ConversationAttachment)
                .where(
                    ConversationAttachment.conversation_id == conv_id,
                    ConversationAttachment.parse_status == "COMPLETED",
                )
                .order_by(ConversationAttachment.created_at.asc())
            )
            att_res = await self.db.execute(att_stmt)
            conversation_attachments = list(att_res.scalars().all())

        # Save user message
        user_msg = Message(
            conversation_id=conv_id,
            role="user",
            content=req.query,
        )
        self.db.add(user_msg)

        # 2. Check knowledge base if requested
        kb = None
        total_chunks = 0
        if req.knowledge_base_id:
            kb_stmt = select(KnowledgeBase).where(KnowledgeBase.id == req.knowledge_base_id)
            kb_res = await self.db.execute(kb_stmt)
            kb = kb_res.scalar_one_or_none()
            if not kb:
                raise KnowledgeBaseNotFoundError(req.knowledge_base_id)

            chunk_count_stmt = (
                select(func.count(DocumentChunk.id))
                .where(DocumentChunk.knowledge_base_id == req.knowledge_base_id)
            )
            total_chunks = (await self.db.execute(chunk_count_stmt)).scalar() or 0

        # Handle empty context scenarios (neither valid KB nor attachments)
        if not kb and not conversation_attachments:
            total_duration = (time.perf_counter() - start_time) * 1000
            answer = "未指定知识库且当前会话无可用附件，请先上传附件或选择知识库。"
            exec_info = ExecutionInfo(
                mode=req.mode,
                retrieved_chunks_count=0,
                retrieval_duration_ms=0.0,
                generation_duration_ms=0.0,
                total_duration_ms=round(total_duration, 2),
                llm_model=self.llm_model_name,
                original_query=req.query,
                rewritten_query=req.query,
                is_rewritten=False,
                model_context_window=self.budget_manager.model_context_window,
                reserved_output_tokens=self.budget_manager.max_output_tokens,
                budget_estimation_method=self.budget_manager.estimator.method,
                attachment_count=0,
            )
            assistant_msg = Message(
                conversation_id=conv_id,
                role="assistant",
                content=answer,
            )
            assistant_msg.execution_info = exec_info.model_dump()
            self.db.add(assistant_msg)
            await self.db.commit()

            return ChatResponse(
                answer=answer,
                citations=[],
                execution_info=exec_info,
                conversation_id=conv_id,
                message_id=assistant_msg.id,
            )

        if kb and total_chunks == 0 and not conversation_attachments:
            total_duration = (time.perf_counter() - start_time) * 1000
            answer = "该知识库目前尚未建立任何文档索引，请先上传并解析文档。"
            exec_info = ExecutionInfo(
                mode=req.mode,
                retrieved_chunks_count=0,
                retrieval_duration_ms=0.0,
                generation_duration_ms=0.0,
                total_duration_ms=round(total_duration, 2),
                llm_model=self.llm_model_name,
                original_query=req.query,
                rewritten_query=req.query,
                is_rewritten=False,
                model_context_window=self.budget_manager.model_context_window,
                reserved_output_tokens=self.budget_manager.max_output_tokens,
                budget_estimation_method=self.budget_manager.estimator.method,
                attachment_count=0,
            )
            assistant_msg = Message(
                conversation_id=conv_id,
                role="assistant",
                content=answer,
            )
            assistant_msg.execution_info = exec_info.model_dump()
            self.db.add(assistant_msg)
            await self.db.commit()

            return ChatResponse(
                answer=answer,
                citations=[],
                execution_info=exec_info,
                conversation_id=conv_id,
                message_id=assistant_msg.id,
            )

        # 3. Attachment retrieval and budgeting (if attachments present)
        att_result = None
        att_chunks = []
        att_duration_ms = 0.0
        if conversation_attachments:
            att_start = time.perf_counter()
            att_result = attachment_retrieval_service.prepare_attachment_context(
                conversation_id=conv_id,
                query=req.query,
                attachments=conversation_attachments,
                system_prompt=self.prompt_builder.system_prompt,
                top_k=req.top_k,
            )
            att_duration_ms = (time.perf_counter() - att_start) * 1000.0
            att_chunks = att_result.selected_chunks

        # 4. Knowledge Base retrieval (if KB configured and chunks exist)
        kb_chunks = []
        retrieval_res = None
        rerank_res = None
        rewrite_result = None
        total_kb_retrieval_duration = 0.0

        if kb and total_chunks > 0:
            # Model consistency check before KB retrieval
            active_provider = self.embedding_client.provider_name
            active_model = self.embedding_client.model_name
            active_dim = self.embedding_client.dimensions

            kb_provider = getattr(kb, "embedding_provider", None) or "local_bge"
            kb_model = getattr(kb, "embedding_model", None) or "BAAI/bge-small-zh-v1.5"
            kb_dim = getattr(kb, "embedding_dimensions", None) or 512

            if active_model != kb_model or active_dim != kb_dim:
                raise VectorModelMismatchError(
                    current_model=f"{active_model} ({active_dim}d, {active_provider})",
                    index_model=f"{kb_model} ({kb_dim}d, {kb_provider})",
                    kb_id=kb.id,
                )

            # Query Rewriting
            rewriter_history = self.budget_manager.prune_history_for_rewriter(history_messages)
            rewrite_result = await self.query_rewriter.rewrite(
                query=req.query,
                history_messages=rewriter_history,
            )
            search_query = rewrite_result.rewritten_query

            # Hybrid Retrieval
            candidate_pool_size = (
                max(req.top_k, settings.RERANKER_CANDIDATE_POOL_SIZE)
                if settings.RERANKER_ENABLED
                else req.top_k
            )
            retrieval_res = await self.hybrid_retriever.search(
                knowledge_base_id=kb.id,
                query=search_query,
                top_k=candidate_pool_size,
            )

            # Cross-Encoder Reranking
            if retrieval_res.chunks:
                rerank_res = await self.reranker.rerank(
                    query=search_query,
                    candidates=retrieval_res.chunks,
                    top_k=req.top_k,
                )
                kb_chunks = rerank_res.chunks
                total_kb_retrieval_duration = retrieval_res.total_duration_ms + rerank_res.duration_ms
            else:
                total_kb_retrieval_duration = retrieval_res.total_duration_ms

        # 5. Combine candidate chunks from KB and Attachments
        combined_candidates: List[ScoredChunk] = []
        if att_chunks and kb_chunks:
            # Both present: sort unified candidates by similarity score descending
            combined_candidates = sorted(
                list(att_chunks) + list(kb_chunks),
                key=lambda c: c.similarity_score,
                reverse=True,
            )
        elif att_chunks:
            combined_candidates = list(att_chunks)
        elif kb_chunks:
            combined_candidates = list(kb_chunks)
        else:
            combined_candidates = []

        # Check if any relevant chunks exist across both sources
        if not combined_candidates:
            gen_start = time.perf_counter()
            answer = "根据当前知识库与附件检索结果，未找到与您提问相关的充分事实证据，无法给出确切回答。"
            gen_duration = (time.perf_counter() - gen_start) * 1000
            total_duration = (time.perf_counter() - start_time) * 1000

            exec_info = ExecutionInfo(
                mode=req.mode,
                retrieved_chunks_count=0,
                retrieval_duration_ms=round(total_kb_retrieval_duration + att_duration_ms, 2),
                generation_duration_ms=round(gen_duration, 2),
                total_duration_ms=round(total_duration, 2),
                llm_model=self.llm_model_name,
                retrieval_mode="hybrid" if kb else "attachment",
                dense_duration_ms=retrieval_res.dense_duration_ms if retrieval_res else None,
                bm25_duration_ms=retrieval_res.bm25_duration_ms if retrieval_res else None,
                rrf_duration_ms=retrieval_res.rrf_duration_ms if retrieval_res else None,
                dense_chunks_count=retrieval_res.dense_chunks_count if retrieval_res else None,
                bm25_chunks_count=retrieval_res.bm25_chunks_count if retrieval_res else None,
                deduplicated_chunks_count=retrieval_res.deduplicated_chunks_count if retrieval_res else None,
                degradation_status=retrieval_res.degradation_status if retrieval_res else "none",
                reranker_enabled=settings.RERANKER_ENABLED if kb else False,
                reranker_model=self.reranker.client.model_name if (settings.RERANKER_ENABLED and kb) else None,
                reranker_duration_ms=rerank_res.duration_ms if rerank_res else 0.0,
                reranker_candidates_in=rerank_res.candidates_in if rerank_res else 0,
                reranker_candidates_out=rerank_res.candidates_out if rerank_res else 0,
                reranker_degraded=rerank_res.degraded if rerank_res else False,
                reranker_degradation_reason=rerank_res.degradation_reason if rerank_res else None,
                original_query=req.query,
                rewritten_query=rewrite_result.rewritten_query if rewrite_result else req.query,
                is_rewritten=rewrite_result.is_rewritten if rewrite_result else False,
                rewrite_duration_ms=rewrite_result.duration_ms if rewrite_result else 0.0,
                rewrite_tokens_used=rewrite_result.tokens_used if rewrite_result else None,
                model_context_window=self.budget_manager.model_context_window,
                reserved_output_tokens=self.budget_manager.max_output_tokens,
                budget_estimation_method=self.budget_manager.estimator.method,
                attachment_count=len(conversation_attachments),
                attachment_strategy=att_result.strategy if att_result else "none",
                attachment_duration_ms=round(att_duration_ms, 2),
                attachment_tokens=att_result.used_tokens if att_result else 0,
                attachment_chunks_kept=0,
                attachment_chunks_pruned=att_result.chunks_pruned if att_result else 0,
            )
            assistant_msg = Message(
                conversation_id=conv_id,
                role="assistant",
                content=answer,
            )
            assistant_msg.execution_info = exec_info.model_dump()
            self.db.add(assistant_msg)
            await self.db.commit()

            return ChatResponse(
                answer=answer,
                citations=[],
                execution_info=exec_info,
                conversation_id=conv_id,
                message_id=assistant_msg.id,
            )

        # Retrieve relevant user memories within budget
        effective_user_id = user_id or settings.DEFAULT_LOCAL_USER_ID
        mem_res = await prepare_memory_context(
            user_id=effective_user_id,
            query=req.query,
            budget=self.budget_manager.memory_token_budget,
            db=self.db,
        )

        # 6. Unified Token Budget Allocation & Context Optimization (M2.4)
        plan = self.budget_manager.allocate_and_assemble(
            query=req.query,
            system_prompt=self.prompt_builder.system_prompt,
            raw_history_messages=history_messages,
            retrieved_chunks=combined_candidates,
            memory_tokens=mem_res.used_memory_tokens,
            memories_kept=mem_res.injected_count,
            memories_pruned=mem_res.pruned_count,
        )

        user_prompt = self.prompt_builder.build_user_prompt(
            query=req.query,
            chunks=plan.kept_chunks,
            history_messages=plan.kept_history,
            memory_context=mem_res.formatted_context,
        )
        gen_start = time.perf_counter()

        answer = await self.llm_client.generate(
            prompt=user_prompt,
            system_prompt=self.prompt_builder.system_prompt,
            temperature=req.temperature,
        )
        gen_duration = (time.perf_counter() - gen_start) * 1000

        # 7. Extract real citations strictly against plan.kept_chunks
        citations = CitationExtractor.extract_citations(answer, plan.kept_chunks)
        total_duration = (time.perf_counter() - start_time) * 1000

        output_tokens = self.budget_manager.estimator.estimate(answer)
        actual_input_tokens = plan.budget_info.actual_input_tokens
        actual_output_tokens = output_tokens
        if hasattr(self.llm_client, "last_usage") and self.llm_client.last_usage:
            actual_input_tokens = self.llm_client.last_usage.get("prompt_tokens")
            actual_output_tokens = self.llm_client.last_usage.get("completion_tokens", output_tokens)

        # Calculate attachment chunks retained in final plan
        att_chunks_kept_count = len([
            c for c in plan.kept_chunks
            if getattr(c, "source_type", None) not in (None, "knowledge_base")
        ])

        exec_info = ExecutionInfo(
            mode=req.mode,
            retrieved_chunks_count=len(plan.kept_chunks),
            retrieval_duration_ms=round(total_kb_retrieval_duration + att_duration_ms, 2),
            generation_duration_ms=round(gen_duration, 2),
            total_duration_ms=round(total_duration, 2),
            llm_model=self.llm_model_name,
            retrieval_mode="hybrid" if kb else "attachment",
            dense_duration_ms=retrieval_res.dense_duration_ms if retrieval_res else None,
            bm25_duration_ms=retrieval_res.bm25_duration_ms if retrieval_res else None,
            rrf_duration_ms=retrieval_res.rrf_duration_ms if retrieval_res else None,
            dense_chunks_count=retrieval_res.dense_chunks_count if retrieval_res else None,
            bm25_chunks_count=retrieval_res.bm25_chunks_count if retrieval_res else None,
            deduplicated_chunks_count=retrieval_res.deduplicated_chunks_count if retrieval_res else None,
            degradation_status=retrieval_res.degradation_status if retrieval_res else "none",
            reranker_enabled=settings.RERANKER_ENABLED if kb else False,
            reranker_model=rerank_res.model_name if rerank_res else None,
            reranker_duration_ms=rerank_res.duration_ms if rerank_res else None,
            reranker_candidates_in=rerank_res.candidates_in if rerank_res else None,
            reranker_candidates_out=rerank_res.candidates_out if rerank_res else None,
            reranker_degraded=rerank_res.degraded if rerank_res else False,
            reranker_degradation_reason=rerank_res.degradation_reason if rerank_res else None,
            original_query=req.query,
            rewritten_query=rewrite_result.rewritten_query if rewrite_result else req.query,
            is_rewritten=rewrite_result.is_rewritten if rewrite_result else False,
            rewrite_duration_ms=rewrite_result.duration_ms if rewrite_result else 0.0,
            rewrite_tokens_used=rewrite_result.tokens_used if rewrite_result else None,
            model_context_window=plan.budget_info.model_context_window,
            estimated_input_tokens=plan.budget_info.estimated_input_tokens,
            actual_input_tokens=actual_input_tokens,
            actual_output_tokens=actual_output_tokens,
            reserved_output_tokens=plan.budget_info.reserved_output_tokens,
            history_tokens=plan.budget_info.history_tokens,
            retrieval_tokens=plan.budget_info.retrieval_tokens,
            history_messages_kept=plan.budget_info.history_messages_kept,
            history_messages_pruned=plan.budget_info.history_messages_pruned,
            retrieval_chunks_kept=plan.budget_info.retrieval_chunks_kept,
            retrieval_chunks_pruned=plan.budget_info.retrieval_chunks_pruned,
            budget_estimation_method=plan.budget_info.budget_estimation_method,
            attachment_count=len(conversation_attachments),
            attachment_strategy=att_result.strategy if att_result else "none",
            attachment_duration_ms=round(att_duration_ms, 2),
            attachment_tokens=att_result.used_tokens if att_result else 0,
            attachment_chunks_kept=att_chunks_kept_count,
            attachment_chunks_pruned=att_result.chunks_pruned if att_result else 0,
            memory_used=mem_res.used_memory_tokens > 0,
            retrieved_memory_count=mem_res.retrieved_count,
            injected_memory_count=mem_res.injected_count,
            memory_tokens=mem_res.used_memory_tokens,
            memory_retrieval_duration_ms=mem_res.duration_ms,
        )

        assistant_msg = Message(
            conversation_id=conv_id,
            role="assistant",
            content=answer,
        )
        assistant_msg.citations = [c.model_dump() for c in citations]
        assistant_msg.execution_info = exec_info.model_dump()
        self.db.add(assistant_msg)

        # Update conversation timestamp & auto title if needed
        conv.updated_at = utc_now()
        if conv.title in ("新建对话", "新会话") or not conv.title.strip():
            conv.title = req.query[:30] + ("..." if len(req.query) > 30 else "")

        await self.db.commit()

        return ChatResponse(
            answer=answer,
            citations=citations,
            execution_info=exec_info,
            conversation_id=conv_id,
            message_id=assistant_msg.id,
        )

    async def execute_stream(
        self,
        req: ChatRequest,
        decision: Optional[RouteDecision] = None,
        user_id: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Execute RAG workflow with SSE token streaming, single retrieval pass,
        temporary attachment retrieval, unified token budget management,
        citation extraction, and message persistence.
        """
        start_time = time.perf_counter()

        # 0. Early query validation against max query token budget (M2.4)
        self.budget_manager.validate_user_query(req.query)

        # 1. Handle conversation creation, validation, and multi-turn history loading
        eff_user_id = user_id or settings.DEFAULT_LOCAL_USER_ID
        history_messages: List[Dict[str, str]] = []
        conv_id = req.conversation_id
        if not conv_id:
            conv = Conversation(
                owner_id=eff_user_id,
                knowledge_base_id=req.knowledge_base_id,
                title=req.query[:30] if len(req.query) > 30 else req.query,
                mode=req.mode,
            )
            self.db.add(conv)
            await self.db.flush()
            conv_id = conv.id
        else:
            conv_stmt = select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.owner_id == eff_user_id,
            )
            conv_res = await self.db.execute(conv_stmt)
            conv = conv_res.scalar_one_or_none()
            if not conv:
                raise ConversationNotFoundError(conv_id)

            if not conv.knowledge_base_id and req.knowledge_base_id:
                conv.knowledge_base_id = req.knowledge_base_id

            history_stmt = (
                select(Message)
                .where(Message.conversation_id == conv.id)
                .order_by(Message.created_at.desc())
                .limit(settings.MAX_HISTORY_TURNS * 2)
            )
            history_res = await self.db.execute(history_stmt)
            raw_history = list(reversed(history_res.scalars().all()))

            total_chars = sum(len(m.content) for m in raw_history)
            while raw_history and total_chars > settings.MAX_HISTORY_CHARS:
                dropped = raw_history.pop(0)
                total_chars -= len(dropped.content)

            for m in raw_history:
                if m.role in ("user", "assistant"):
                    cleaned = (
                        m.content.replace("\n\n[回答已被用户中止]", "")
                        .replace("[回答已被用户中止]", "")
                        .strip()
                    )
                    if cleaned:
                        history_messages.append({"role": m.role, "content": cleaned})

        # Load completed attachments for current conversation
        conversation_attachments: List[ConversationAttachment] = []
        if conv_id:
            att_stmt = (
                select(ConversationAttachment)
                .where(
                    ConversationAttachment.conversation_id == conv_id,
                    ConversationAttachment.parse_status == "COMPLETED",
                )
                .order_by(ConversationAttachment.created_at.asc())
            )
            att_res = await self.db.execute(att_stmt)
            conversation_attachments = list(att_res.scalars().all())

        # Save user message
        user_msg = Message(
            conversation_id=conv_id,
            role="user",
            content=req.query,
        )
        self.db.add(user_msg)
        await self.db.flush()

        # Check knowledge base if requested
        kb = None
        total_chunks = 0
        if req.knowledge_base_id:
            kb_stmt = select(KnowledgeBase).where(KnowledgeBase.id == req.knowledge_base_id)
            kb_res = await self.db.execute(kb_stmt)
            kb = kb_res.scalar_one_or_none()
            if not kb:
                raise KnowledgeBaseNotFoundError(req.knowledge_base_id)

            chunk_count_stmt = (
                select(func.count(DocumentChunk.id))
                .where(DocumentChunk.knowledge_base_id == req.knowledge_base_id)
            )
            total_chunks = (await self.db.execute(chunk_count_stmt)).scalar() or 0

        chunks: List[Any] = []
        retrieval_duration = 0.0
        retrieval_res: Optional[HybridRetrievalResult] = None
        rerank_res: Optional[RerankResult] = None
        gen_start: Optional[float] = None
        ttft_ms: Optional[float] = None
        accumulated_answer = ""
        rewrite_result: Optional[Any] = None
        plan: Optional[Any] = None
        att_result = None
        att_duration_ms = 0.0

        try:
            # Emit start event
            yield {
                "event": "start",
                "data": {
                    "conversation_id": conv_id,
                    "message_id": user_msg.id,
                },
            }

            if decision:
                yield {
                    "event": "route",
                    "data": decision.model_dump(),
                }

            # Handle empty context scenarios (neither valid KB nor attachments)
            if not kb and not conversation_attachments:
                total_duration = (time.perf_counter() - start_time) * 1000
                answer = "未指定知识库且当前会话无可用附件，请先上传附件或选择知识库。"
                exec_info = ExecutionInfo(
                    mode=req.mode,
                    retrieved_chunks_count=0,
                    retrieval_duration_ms=0.0,
                    generation_duration_ms=0.0,
                    total_duration_ms=round(total_duration, 2),
                    llm_model=self.llm_model_name,
                    original_query=req.query,
                    rewritten_query=req.query,
                    is_rewritten=False,
                    model_context_window=self.budget_manager.model_context_window,
                    reserved_output_tokens=self.budget_manager.max_output_tokens,
                    budget_estimation_method=self.budget_manager.estimator.method,
                    attachment_count=0,
                )
                yield {
                    "event": "retrieval",
                    "data": {
                        "retrieved_chunks_count": 0,
                        "retrieval_duration_ms": 0.0,
                        "retrieval_mode": "none",
                        "reranker_enabled": False,
                        "reranker_candidates_in": 0,
                        "reranker_candidates_out": 0,
                    },
                }
                yield {"event": "token", "data": {"delta": answer}}

                assistant_msg = Message(
                    conversation_id=conv_id,
                    role="assistant",
                    content=answer,
                )
                assistant_msg.execution_info = exec_info.model_dump()
                self.db.add(assistant_msg)
                await self.db.commit()

                yield {"event": "citations", "data": []}
                yield {"event": "trace", "data": exec_info.model_dump()}
                yield {"event": "done", "data": {"message_id": assistant_msg.id}}
                return

            # Handle empty KB index when no attachments exist
            if kb and total_chunks == 0 and not conversation_attachments:
                total_duration = (time.perf_counter() - start_time) * 1000
                answer = "该知识库目前尚未建立任何文档索引，请先上传并解析文档。"
                exec_info = ExecutionInfo(
                    mode=req.mode,
                    retrieved_chunks_count=0,
                    retrieval_duration_ms=0.0,
                    generation_duration_ms=0.0,
                    total_duration_ms=round(total_duration, 2),
                    llm_model=self.llm_model_name,
                    original_query=req.query,
                    rewritten_query=req.query,
                    is_rewritten=False,
                    model_context_window=self.budget_manager.model_context_window,
                    reserved_output_tokens=self.budget_manager.max_output_tokens,
                    budget_estimation_method=self.budget_manager.estimator.method,
                    attachment_count=0,
                )
                yield {
                    "event": "retrieval",
                    "data": {
                        "retrieved_chunks_count": 0,
                        "retrieval_duration_ms": 0.0,
                    },
                }
                yield {"event": "token", "data": {"delta": answer}}

                assistant_msg = Message(
                    conversation_id=conv_id,
                    role="assistant",
                    content=answer,
                )
                assistant_msg.execution_info = exec_info.model_dump()
                self.db.add(assistant_msg)
                await self.db.commit()

                yield {"event": "citations", "data": []}
                yield {"event": "trace", "data": exec_info.model_dump()}
                yield {"event": "done", "data": {"message_id": assistant_msg.id}}
                return

            # 2. Attachment retrieval (if attachments present)
            att_chunks = []
            if conversation_attachments:
                yield {
                    "event": "attachment_context_started",
                    "data": {
                        "conversation_id": conv_id,
                        "attachment_count": len(conversation_attachments),
                        "attachment_ids": [a.id for a in conversation_attachments],
                    },
                }

                att_start = time.perf_counter()
                att_result = attachment_retrieval_service.prepare_attachment_context(
                    conversation_id=conv_id,
                    query=req.query,
                    attachments=conversation_attachments,
                    system_prompt=self.prompt_builder.system_prompt,
                    top_k=req.top_k,
                )
                att_duration_ms = (time.perf_counter() - att_start) * 1000.0
                att_chunks = att_result.selected_chunks

                yield {
                    "event": "attachment_retrieval_completed",
                    "data": {
                        "conversation_id": conv_id,
                        "strategy": att_result.strategy,
                        "selected_chunks_count": len(att_chunks),
                        "chunks_pruned": att_result.chunks_pruned,
                        "duration_ms": round(att_duration_ms, 2),
                        "used_tokens": att_result.used_tokens,
                    },
                }

                yield {
                    "event": "attachment_context_completed",
                    "data": {
                        "conversation_id": conv_id,
                        "attachment_count": len(conversation_attachments),
                        "total_chunks_selected": len(att_chunks),
                        "strategy": att_result.strategy,
                    },
                }

            # 3. Knowledge Base retrieval (if KB configured and chunks exist)
            kb_chunks = []
            total_kb_retrieval_duration = 0.0
            search_query = req.query

            if kb and total_chunks > 0:
                # Model consistency check
                active_provider = self.embedding_client.provider_name
                active_model = self.embedding_client.model_name
                active_dim = self.embedding_client.dimensions

                kb_provider = getattr(kb, "embedding_provider", None) or "local_bge"
                kb_model = getattr(kb, "embedding_model", None) or "BAAI/bge-small-zh-v1.5"
                kb_dim = getattr(kb, "embedding_dimensions", None) or 512

                if active_model != kb_model or active_dim != kb_dim:
                    raise VectorModelMismatchError(
                        current_model=f"{active_model} ({active_dim}d, {active_provider})",
                        index_model=f"{kb_model} ({kb_dim}d, {kb_provider})",
                        kb_id=kb.id,
                    )

                # Context-aware Query Rewriting (M2.3)
                rewriter_history = self.budget_manager.prune_history_for_rewriter(history_messages)
                rewrite_result = await self.query_rewriter.rewrite(
                    query=req.query,
                    history_messages=rewriter_history,
                )
                search_query = rewrite_result.rewritten_query

                yield {
                    "event": "query_rewrite",
                    "data": {
                        "original_query": rewrite_result.original_query,
                        "rewritten_query": rewrite_result.rewritten_query,
                        "is_rewritten": rewrite_result.is_rewritten,
                        "duration_ms": rewrite_result.duration_ms,
                        "tokens_used": rewrite_result.tokens_used,
                        "fallback": rewrite_result.fallback,
                        "fallback_reason": rewrite_result.fallback_reason,
                    },
                }

                # Parallel Hybrid Retrieval (Dense + BM25 merged via RRF) (M3.2)
                candidate_pool_size = (
                    max(req.top_k, settings.RERANKER_CANDIDATE_POOL_SIZE)
                    if settings.RERANKER_ENABLED
                    else req.top_k
                )
                retrieval_res = await self.hybrid_retriever.search(
                    knowledge_base_id=kb.id,
                    query=search_query,
                    top_k=candidate_pool_size,
                )

                if retrieval_res.chunks:
                    rerank_res = await self.reranker.rerank(
                        query=search_query,
                        candidates=retrieval_res.chunks,
                        top_k=req.top_k,
                    )
                    kb_chunks = rerank_res.chunks
                    total_kb_retrieval_duration = retrieval_res.total_duration_ms + rerank_res.duration_ms
                else:
                    total_kb_retrieval_duration = retrieval_res.total_duration_ms

            # 4. Combine candidate chunks from KB and Attachments
            combined_candidates: List[ScoredChunk] = []
            if att_chunks and kb_chunks:
                combined_candidates = sorted(
                    list(att_chunks) + list(kb_chunks),
                    key=lambda c: c.similarity_score,
                    reverse=True,
                )
            elif att_chunks:
                combined_candidates = list(att_chunks)
            elif kb_chunks:
                combined_candidates = list(kb_chunks)
            else:
                combined_candidates = []

            # Handle no relevant chunks retrieved across both sources
            if not combined_candidates:
                gen_start = time.perf_counter()
                answer = "根据当前知识库与附件检索结果，未找到与您提问相关的充分事实证据，无法给出确切回答。"
                gen_duration = (time.perf_counter() - gen_start) * 1000
                total_duration = (time.perf_counter() - start_time) * 1000

                exec_info = ExecutionInfo(
                    mode=req.mode,
                    retrieved_chunks_count=0,
                    retrieval_duration_ms=round(total_kb_retrieval_duration + att_duration_ms, 2),
                    generation_duration_ms=round(gen_duration, 2),
                    total_duration_ms=round(total_duration, 2),
                    llm_model=self.llm_model_name,
                    retrieval_mode="hybrid" if kb else "attachment",
                    dense_duration_ms=retrieval_res.dense_duration_ms if retrieval_res else None,
                    bm25_duration_ms=retrieval_res.bm25_duration_ms if retrieval_res else None,
                    rrf_duration_ms=retrieval_res.rrf_duration_ms if retrieval_res else None,
                    dense_chunks_count=retrieval_res.dense_chunks_count if retrieval_res else None,
                    bm25_chunks_count=retrieval_res.bm25_chunks_count if retrieval_res else None,
                    deduplicated_chunks_count=retrieval_res.deduplicated_chunks_count if retrieval_res else None,
                    degradation_status=retrieval_res.degradation_status if retrieval_res else "none",
                    reranker_enabled=settings.RERANKER_ENABLED if kb else False,
                    reranker_model=self.reranker.client.model_name if (settings.RERANKER_ENABLED and kb) else None,
                    reranker_duration_ms=rerank_res.duration_ms if rerank_res else 0.0,
                    reranker_candidates_in=rerank_res.candidates_in if rerank_res else 0,
                    reranker_candidates_out=rerank_res.candidates_out if rerank_res else 0,
                    reranker_degraded=rerank_res.degraded if rerank_res else False,
                    reranker_degradation_reason=rerank_res.degradation_reason if rerank_res else None,
                    original_query=req.query,
                    rewritten_query=rewrite_result.rewritten_query if rewrite_result else req.query,
                    is_rewritten=rewrite_result.is_rewritten if rewrite_result else False,
                    rewrite_duration_ms=rewrite_result.duration_ms if rewrite_result else 0.0,
                    rewrite_tokens_used=rewrite_result.tokens_used if rewrite_result else None,
                    model_context_window=self.budget_manager.model_context_window,
                    reserved_output_tokens=self.budget_manager.max_output_tokens,
                    budget_estimation_method=self.budget_manager.estimator.method,
                    attachment_count=len(conversation_attachments),
                    attachment_strategy=att_result.strategy if att_result else "none",
                    attachment_duration_ms=round(att_duration_ms, 2),
                    attachment_tokens=att_result.used_tokens if att_result else 0,
                    attachment_chunks_kept=0,
                    attachment_chunks_pruned=att_result.chunks_pruned if att_result else 0,
                )
                yield {
                    "event": "retrieval",
                    "data": {
                        "retrieved_chunks_count": 0,
                        "retrieval_duration_ms": round(total_kb_retrieval_duration + att_duration_ms, 2),
                        "retrieval_mode": "hybrid" if kb else "attachment",
                        "reranker_enabled": settings.RERANKER_ENABLED if kb else False,
                        "reranker_candidates_in": 0,
                        "reranker_candidates_out": 0,
                    },
                }
                yield {"event": "token", "data": {"delta": answer}}

                assistant_msg = Message(
                    conversation_id=conv_id,
                    role="assistant",
                    content=answer,
                )
                assistant_msg.execution_info = exec_info.model_dump()
                self.db.add(assistant_msg)
                await self.db.commit()

                yield {"event": "citations", "data": []}
                yield {"event": "trace", "data": exec_info.model_dump()}
                yield {"event": "done", "data": {"message_id": assistant_msg.id}}
                return

            # Emit unified retrieval event
            yield {
                "event": "retrieval",
                "data": {
                    "retrieved_chunks_count": len(combined_candidates),
                    "retrieval_duration_ms": round(total_kb_retrieval_duration + att_duration_ms, 2),
                    "retrieval_mode": "hybrid" if kb else "attachment",
                    "dense_duration_ms": retrieval_res.dense_duration_ms if retrieval_res else None,
                    "bm25_duration_ms": retrieval_res.bm25_duration_ms if retrieval_res else None,
                    "rrf_duration_ms": retrieval_res.rrf_duration_ms if retrieval_res else None,
                    "dense_chunks_count": retrieval_res.dense_chunks_count if retrieval_res else None,
                    "bm25_chunks_count": retrieval_res.bm25_chunks_count if retrieval_res else None,
                    "deduplicated_chunks_count": retrieval_res.deduplicated_chunks_count if retrieval_res else None,
                    "degradation_status": retrieval_res.degradation_status if retrieval_res else "none",
                    "query_used": search_query if kb else req.query,
                    "reranker_enabled": settings.RERANKER_ENABLED if kb else False,
                    "reranker_model": rerank_res.model_name if rerank_res else None,
                    "reranker_duration_ms": rerank_res.duration_ms if rerank_res else None,
                    "reranker_candidates_in": rerank_res.candidates_in if rerank_res else None,
                    "reranker_candidates_out": rerank_res.candidates_out if rerank_res else None,
                    "reranker_degraded": rerank_res.degraded if rerank_res else False,
                    "reranker_degradation_reason": rerank_res.degradation_reason if rerank_res else None,
                    "attachment_count": len(conversation_attachments),
                    "attachment_strategy": att_result.strategy if att_result else "none",
                    "attachment_duration_ms": round(att_duration_ms, 2),
                },
            }

            # Retrieve relevant user memories within budget
            effective_user_id = user_id or settings.DEFAULT_LOCAL_USER_ID
            mem_res = await prepare_memory_context(
                user_id=effective_user_id,
                query=req.query,
                budget=self.budget_manager.memory_token_budget,
                db=self.db,
            )

            # 5. Unified Token Budget Allocation & Context Optimization (M2.4)
            plan = self.budget_manager.allocate_and_assemble(
                query=req.query,
                system_prompt=self.prompt_builder.system_prompt,
                raw_history_messages=history_messages,
                retrieved_chunks=combined_candidates,
                memory_tokens=mem_res.used_memory_tokens,
                memories_kept=mem_res.injected_count,
                memories_pruned=mem_res.pruned_count,
            )

            # Emit context_budget event
            yield {
                "event": "context_budget",
                "data": plan.budget_info.model_dump(),
            }

            # 6. Stream tokens from LLM using budget-optimized context and chunks
            user_prompt = self.prompt_builder.build_user_prompt(
                query=req.query,
                chunks=plan.kept_chunks,
                history_messages=plan.kept_history,
                memory_context=mem_res.formatted_context,
            )
            gen_start = time.perf_counter()

            async for token in self.llm_client.generate_stream(
                prompt=user_prompt,
                system_prompt=self.prompt_builder.system_prompt,
                temperature=req.temperature,
            ):
                if ttft_ms is None:
                    ttft_ms = round((time.perf_counter() - gen_start) * 1000, 2)
                accumulated_answer += token
                yield {"event": "token", "data": {"delta": token}}

            # 7. Post-processing: Citation extraction and DB persistence
            gen_duration = (time.perf_counter() - gen_start) * 1000
            total_duration = (time.perf_counter() - start_time) * 1000

            citations = CitationExtractor.extract_citations(accumulated_answer, plan.kept_chunks)
            output_tokens = self.budget_manager.estimator.estimate(accumulated_answer)
            actual_input_tokens = plan.budget_info.actual_input_tokens
            actual_output_tokens = output_tokens
            if hasattr(self.llm_client, "last_usage") and self.llm_client.last_usage:
                actual_input_tokens = self.llm_client.last_usage.get("prompt_tokens")
                actual_output_tokens = self.llm_client.last_usage.get("completion_tokens", output_tokens)

            # Calculate attachment chunks retained in final plan
            att_chunks_kept_count = len([
                c for c in plan.kept_chunks
                if getattr(c, "source_type", None) not in (None, "knowledge_base")
            ])

            exec_info = ExecutionInfo(
                mode=req.mode,
                retrieved_chunks_count=len(plan.kept_chunks),
                retrieval_duration_ms=round(total_kb_retrieval_duration + att_duration_ms, 2),
                generation_duration_ms=round(gen_duration, 2),
                total_duration_ms=round(total_duration, 2),
                llm_model=self.llm_model_name,
                ttft_ms=ttft_ms,
                retrieval_mode="hybrid" if kb else "attachment",
                dense_duration_ms=retrieval_res.dense_duration_ms if retrieval_res else None,
                bm25_duration_ms=retrieval_res.bm25_duration_ms if retrieval_res else None,
                rrf_duration_ms=retrieval_res.rrf_duration_ms if retrieval_res else None,
                dense_chunks_count=retrieval_res.dense_chunks_count if retrieval_res else None,
                bm25_chunks_count=retrieval_res.bm25_chunks_count if retrieval_res else None,
                deduplicated_chunks_count=retrieval_res.deduplicated_chunks_count if retrieval_res else None,
                degradation_status=retrieval_res.degradation_status if retrieval_res else "none",
                reranker_enabled=settings.RERANKER_ENABLED if kb else False,
                reranker_model=rerank_res.model_name if rerank_res else None,
                reranker_duration_ms=rerank_res.duration_ms if rerank_res else None,
                reranker_candidates_in=rerank_res.candidates_in if rerank_res else None,
                reranker_candidates_out=rerank_res.candidates_out if rerank_res else None,
                reranker_degraded=rerank_res.degraded if rerank_res else False,
                reranker_degradation_reason=rerank_res.degradation_reason if rerank_res else None,
                original_query=req.query,
                rewritten_query=rewrite_result.rewritten_query if rewrite_result else req.query,
                is_rewritten=rewrite_result.is_rewritten if rewrite_result else False,
                rewrite_duration_ms=rewrite_result.duration_ms if rewrite_result else 0.0,
                rewrite_tokens_used=rewrite_result.tokens_used if rewrite_result else None,
                model_context_window=plan.budget_info.model_context_window,
                estimated_input_tokens=plan.budget_info.estimated_input_tokens,
                actual_input_tokens=actual_input_tokens,
                actual_output_tokens=actual_output_tokens,
                reserved_output_tokens=plan.budget_info.reserved_output_tokens,
                history_tokens=plan.budget_info.history_tokens,
                retrieval_tokens=plan.budget_info.retrieval_tokens,
                history_messages_kept=plan.budget_info.history_messages_kept,
                history_messages_pruned=plan.budget_info.history_messages_pruned,
                retrieval_chunks_kept=plan.budget_info.retrieval_chunks_kept,
                retrieval_chunks_pruned=plan.budget_info.retrieval_chunks_pruned,
                budget_estimation_method=plan.budget_info.budget_estimation_method,
                attachment_count=len(conversation_attachments),
                attachment_strategy=att_result.strategy if att_result else "none",
                attachment_duration_ms=round(att_duration_ms, 2),
                attachment_tokens=att_result.used_tokens if att_result else 0,
                attachment_chunks_kept=att_chunks_kept_count,
                attachment_chunks_pruned=att_result.chunks_pruned if att_result else 0,
                memory_used=mem_res.used_memory_tokens > 0,
                retrieved_memory_count=mem_res.retrieved_count,
                injected_memory_count=mem_res.injected_count,
                memory_tokens=mem_res.used_memory_tokens,
                memory_retrieval_duration_ms=mem_res.duration_ms,
            )

            assistant_msg = Message(
                conversation_id=conv_id,
                role="assistant",
                content=accumulated_answer,
            )
            assistant_msg.citations = [c.model_dump() for c in citations]
            assistant_msg.execution_info = exec_info.model_dump()
            self.db.add(assistant_msg)

            # Update conversation timestamp & auto title if needed
            conv.updated_at = utc_now()
            if conv.title in ("新建对话", "新会话") or not conv.title.strip():
                conv.title = req.query[:30] + ("..." if len(req.query) > 30 else "")

            await self.db.commit()

            # Trigger automatic memory extraction in background
            asyncio.create_task(
                run_memory_extraction_task(
                    user_id=effective_user_id,
                    query=req.query,
                    answer=accumulated_answer,
                    conversation_id=conv_id,
                    message_id=assistant_msg.id,
                )
            )

            # Emit citations, trace, and done events
            yield {"event": "citations", "data": [c.model_dump() for c in citations]}
            yield {"event": "trace", "data": exec_info.model_dump()}
            yield {"event": "done", "data": {"message_id": assistant_msg.id}}

        except asyncio.CancelledError:
            # Client disconnected or cancelled mid-stream
            total_duration = (time.perf_counter() - start_time) * 1000
            gen_duration = (time.perf_counter() - gen_start) * 1000 if gen_start else 0.0
            saved_content = (
                accumulated_answer + "\n\n[回答已被用户中止]"
                if accumulated_answer
                else "[回答已被用户中止]"
            )
            final_chunks = plan.kept_chunks if plan else chunks
            partial_citations = (
                CitationExtractor.extract_citations(accumulated_answer, final_chunks)
                if accumulated_answer and final_chunks
                else []
            )
            output_tokens = (
                self.budget_manager.estimator.estimate(accumulated_answer)
                if accumulated_answer
                else 0
            )

            att_chunks_kept_count = 0
            if plan and plan.kept_chunks:
                att_chunks_kept_count = len([
                    c for c in plan.kept_chunks
                    if getattr(c, "source_type", None) not in (None, "knowledge_base")
                ])

            exec_info = ExecutionInfo(
                mode=req.mode,
                retrieved_chunks_count=len(final_chunks),
                retrieval_duration_ms=round((retrieval_res.total_duration_ms if retrieval_res else 0.0) + att_duration_ms, 2),
                generation_duration_ms=round(gen_duration, 2),
                total_duration_ms=round(total_duration, 2),
                llm_model=self.llm_model_name,
                ttft_ms=ttft_ms,
                retrieval_mode="hybrid" if kb else "attachment",
                dense_duration_ms=retrieval_res.dense_duration_ms if retrieval_res else None,
                bm25_duration_ms=retrieval_res.bm25_duration_ms if retrieval_res else None,
                rrf_duration_ms=retrieval_res.rrf_duration_ms if retrieval_res else None,
                dense_chunks_count=retrieval_res.dense_chunks_count if retrieval_res else None,
                bm25_chunks_count=retrieval_res.bm25_chunks_count if retrieval_res else None,
                deduplicated_chunks_count=retrieval_res.deduplicated_chunks_count if retrieval_res else None,
                degradation_status=retrieval_res.degradation_status if retrieval_res else "none",
                reranker_enabled=settings.RERANKER_ENABLED if kb else False,
                reranker_model=rerank_res.model_name if rerank_res else None,
                reranker_duration_ms=rerank_res.duration_ms if rerank_res else None,
                reranker_candidates_in=rerank_res.candidates_in if rerank_res else None,
                reranker_candidates_out=rerank_res.candidates_out if rerank_res else None,
                reranker_degraded=rerank_res.degraded if rerank_res else False,
                reranker_degradation_reason=rerank_res.degradation_reason if rerank_res else None,
                original_query=req.query,
                rewritten_query=rewrite_result.rewritten_query if rewrite_result else req.query,
                is_rewritten=rewrite_result.is_rewritten if rewrite_result else False,
                rewrite_duration_ms=rewrite_result.duration_ms if rewrite_result else 0.0,
                rewrite_tokens_used=rewrite_result.tokens_used if rewrite_result else None,
                model_context_window=plan.budget_info.model_context_window if plan else self.budget_manager.model_context_window,
                estimated_input_tokens=plan.budget_info.estimated_input_tokens if plan else None,
                actual_input_tokens=plan.budget_info.actual_input_tokens if plan else None,
                actual_output_tokens=output_tokens,
                reserved_output_tokens=plan.budget_info.reserved_output_tokens if plan else self.budget_manager.max_output_tokens,
                history_tokens=plan.budget_info.history_tokens if plan else None,
                retrieval_tokens=plan.budget_info.retrieval_tokens if plan else None,
                history_messages_kept=plan.budget_info.history_messages_kept if plan else None,
                history_messages_pruned=plan.budget_info.history_messages_pruned if plan else None,
                retrieval_chunks_kept=plan.budget_info.retrieval_chunks_kept if plan else None,
                retrieval_chunks_pruned=plan.budget_info.retrieval_chunks_pruned if plan else None,
                budget_estimation_method=plan.budget_info.budget_estimation_method if plan else self.budget_manager.estimator.method,
                attachment_count=len(conversation_attachments),
                attachment_strategy=att_result.strategy if att_result else "none",
                attachment_duration_ms=round(att_duration_ms, 2),
                attachment_tokens=att_result.used_tokens if att_result else 0,
                attachment_chunks_kept=att_chunks_kept_count,
                attachment_chunks_pruned=att_result.chunks_pruned if att_result else 0,
            )
            assistant_msg = Message(
                conversation_id=conv_id,
                role="assistant",
                content=saved_content,
            )
            assistant_msg.citations = [c.model_dump() for c in partial_citations]
            assistant_msg.execution_info = exec_info.model_dump()
            self.db.add(assistant_msg)
            await self.db.commit()
            raise
        except Exception as exc:
            logger.error(f"Error during RAG stream generation: {exc}", exc_info=True)
            yield {"event": "error", "data": {"detail": str(exc)}}
            return
