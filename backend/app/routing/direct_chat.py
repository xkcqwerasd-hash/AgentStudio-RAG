import asyncio
from datetime import datetime, timedelta, timezone
import time
from typing import Any, AsyncIterator, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import ConversationNotFoundError
from app.core.logging import logger
from app.llm.base import BaseLLMClient
from app.llm.openai_client import get_llm_client
from app.memory import prepare_memory_context, run_memory_extraction_task
from app.models.conversation import Conversation, Message
from app.rag.context_budget import ContextBudgetManager
from app.rag.prompt_builder import PromptBuilder
from app.routing.schemas import RouteDecision
from app.schemas.chat import ChatRequest, ChatResponse, ExecutionInfo

DIRECT_SYSTEM_PROMPT = """你是由 AgentStudio 驱动的企业级智能 AI 助手。
请根据自身广泛的通用知识和专业能力，清晰、严谨、客观、准确地回答用户的问题。
如果用户在多轮对话中指代前文，请结合前序对话历史连贯解答。
排版请使用结构良好、清晰易读的 Markdown 格式。"""


class DirectChatEngine:
    """
    Direct LLM Execution Engine for general questions, chitchat, and conversational follow-ups.
    Completely bypasses VectorRetriever, BM25Retriever, Reranker, and Tool calling.
    Guarantees 0-retrieval overhead while preserving multi-turn history, streaming, and DB persistence.
    """

    def __init__(
        self,
        db: AsyncSession,
        llm_client: Optional[BaseLLMClient] = None,
        llm_model: Optional[str] = None,
    ):
        self.db = db
        self.llm_client = llm_client or get_llm_client()
        self.llm_model_name = (
            llm_model
            or getattr(self.llm_client, "model", None)
            or settings.LLM_MODEL
        )
        self.budget_manager = ContextBudgetManager()
        self.prompt_builder = PromptBuilder()

    async def _resolve_conversation_and_history(
        self, req: ChatRequest, user_id: Optional[str] = None
    ) -> tuple[str, List[Dict[str, str]]]:
        """Resolve conversation ID and load clean multi-turn history messages, scoped to user_id."""
        eff_user_id = user_id or settings.DEFAULT_LOCAL_USER_ID
        conv_id = req.conversation_id
        history_messages: List[Dict[str, str]] = []

        if not conv_id:
            conv = Conversation(
                owner_id=eff_user_id,
                knowledge_base_id=req.knowledge_base_id or None,
                title=req.query[:30] if len(req.query) > 30 else req.query,
                mode=req.mode or "workflow",
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

            # Load recent messages
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

        return conv_id, history_messages

    def _build_full_prompt(
        self,
        query: str,
        history_messages: List[Dict[str, str]],
        memory_context: Optional[str] = None,
    ) -> str:
        """Assemble user prompt including optional memory context and conversation history."""
        parts = []
        if memory_context:
            parts.append(memory_context)

        history_str = self.prompt_builder.build_history_context(history_messages)
        if history_str:
            parts.append(history_str)

        if memory_context or history_str:
            parts.append(f"【用户当前提问】：\n{query}")
        else:
            parts.append(query)

        return "\n\n".join(parts)

    async def execute(
        self,
        req: ChatRequest,
        decision: RouteDecision,
        user_id: Optional[str] = None,
    ) -> ChatResponse:
        """Synchronous non-streaming execution for direct LLM chat."""
        start_time = time.perf_counter()
        self.budget_manager.validate_user_query(req.query)

        conv_id, history_messages = await self._resolve_conversation_and_history(req, user_id=user_id)

        now_dt = utc_now()
        user_msg = Message(
            conversation_id=conv_id,
            role="user",
            content=req.query,
            created_at=now_dt,
        )
        self.db.add(user_msg)
        await self.db.flush()

        # Retrieve relevant user memories within budget
        effective_user_id = user_id or settings.DEFAULT_LOCAL_USER_ID
        mem_res = await prepare_memory_context(
            user_id=effective_user_id,
            query=req.query,
            budget=self.budget_manager.memory_token_budget,
            db=self.db,
        )

        full_prompt = self._build_full_prompt(
            req.query,
            history_messages,
            memory_context=mem_res.formatted_context,
        )

        gen_start = time.perf_counter()
        answer = await self.llm_client.generate(
            prompt=full_prompt,
            system_prompt=DIRECT_SYSTEM_PROMPT,
            temperature=req.temperature,
        )
        gen_duration = (time.perf_counter() - gen_start) * 1000.0
        total_duration = (time.perf_counter() - start_time) * 1000.0

        exec_info = ExecutionInfo(
            mode="direct",
            intent=decision.intent.value,
            execution_path=decision.execution_path.value,
            route_reason=decision.reason,
            retrieved_chunks_count=0,
            retrieval_duration_ms=0.0,
            generation_duration_ms=round(gen_duration, 2),
            total_duration_ms=round(total_duration, 2),
            llm_model=self.llm_model_name,
            original_query=req.query,
            rewritten_query=req.query,
            is_rewritten=False,
            model_context_window=self.budget_manager.model_context_window,
            reserved_output_tokens=self.budget_manager.max_output_tokens,
            budget_estimation_method=self.budget_manager.estimator.method,
            attachment_count=0,
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
            created_at=now_dt + timedelta(milliseconds=1),
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

    async def execute_stream(
        self,
        req: ChatRequest,
        decision: RouteDecision,
        user_id: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Streaming execution emitting SSE events for direct LLM chat."""
        start_time = time.perf_counter()
        self.budget_manager.validate_user_query(req.query)

        conv_id, history_messages = await self._resolve_conversation_and_history(req, user_id=user_id)

        now_dt = utc_now()
        user_msg = Message(
            conversation_id=conv_id,
            role="user",
            content=req.query,
            created_at=now_dt,
        )
        self.db.add(user_msg)
        await self.db.flush()

        # 1. Start event
        yield {
            "event": "start",
            "data": {
                "conversation_id": conv_id,
                "message_id": user_msg.id,
            },
        }

        # 2. Route event
        yield {
            "event": "route",
            "data": decision.model_dump(),
        }

        # Retrieve relevant user memories within budget
        effective_user_id = user_id or settings.DEFAULT_LOCAL_USER_ID
        mem_res = await prepare_memory_context(
            user_id=effective_user_id,
            query=req.query,
            budget=self.budget_manager.memory_token_budget,
            db=self.db,
        )

        full_prompt = self._build_full_prompt(
            req.query,
            history_messages,
            memory_context=mem_res.formatted_context,
        )

        accumulated_answer = ""
        gen_start = time.perf_counter()
        ttft_ms: Optional[float] = None

        try:
            async for token in self.llm_client.generate_stream(
                prompt=full_prompt,
                system_prompt=DIRECT_SYSTEM_PROMPT,
                temperature=req.temperature,
            ):
                if ttft_ms is None:
                    ttft_ms = round((time.perf_counter() - gen_start) * 1000.0, 2)
                accumulated_answer += token
                yield {"event": "token", "data": {"delta": token}}

        except asyncio.CancelledError:
            logger.info(f"Direct LLM streaming cancelled by client for conv {conv_id}")
            if accumulated_answer.strip():
                accumulated_answer += "\n\n[回答已被用户中止]"
            else:
                accumulated_answer = "[回答已被用户中止]"

            total_duration = (time.perf_counter() - start_time) * 1000.0
            exec_info = ExecutionInfo(
                mode="direct",
                intent=decision.intent.value,
                execution_path=decision.execution_path.value,
                route_reason=decision.reason,
                retrieved_chunks_count=0,
                retrieval_duration_ms=0.0,
                generation_duration_ms=round((time.perf_counter() - gen_start) * 1000.0, 2),
                total_duration_ms=round(total_duration, 2),
                llm_model=self.llm_model_name,
                ttft_ms=ttft_ms,
                original_query=req.query,
                rewritten_query=req.query,
                is_rewritten=False,
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
            assistant_msg.execution_info = exec_info.model_dump()
            self.db.add(assistant_msg)
            await self.db.commit()
            raise

        gen_duration = (time.perf_counter() - gen_start) * 1000.0
        total_duration = (time.perf_counter() - start_time) * 1000.0

        exec_info = ExecutionInfo(
            mode="direct",
            intent=decision.intent.value,
            execution_path=decision.execution_path.value,
            route_reason=decision.reason,
            retrieved_chunks_count=0,
            retrieval_duration_ms=0.0,
            generation_duration_ms=round(gen_duration, 2),
            total_duration_ms=round(total_duration, 2),
            llm_model=self.llm_model_name,
            ttft_ms=ttft_ms,
            original_query=req.query,
            rewritten_query=req.query,
            is_rewritten=False,
            model_context_window=self.budget_manager.model_context_window,
            reserved_output_tokens=self.budget_manager.max_output_tokens,
            budget_estimation_method=self.budget_manager.estimator.method,
            attachment_count=0,
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
            created_at=now_dt + timedelta(milliseconds=1),
        )
        assistant_msg.execution_info = exec_info.model_dump()
        self.db.add(assistant_msg)
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

        # 3. Final events
        yield {"event": "citations", "data": []}
        yield {"event": "trace", "data": exec_info.model_dump()}
        yield {"event": "done", "data": {"message_id": assistant_msg.id}}
