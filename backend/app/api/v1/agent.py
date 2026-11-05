import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.agent.runtime.runtime import LangGraphAgentRuntime
from app.agent.tools.registry import get_default_tool_registry
from app.api.deps import get_db
from app.core.auth import get_current_user, get_current_user_id
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import (
    AgentDefinitionNotFoundError,
    AgentRunNotFoundError,
    ConversationNotFoundError,
    InvalidToolConfigurationError,
    KnowledgeBaseNotFoundError,
)
from app.core.logging import logger
from app.llm.factory import resolve_llm_client, validate_tool_calling_capability
from app.memory import prepare_memory_context, run_memory_extraction_task
from app.db.session import async_session_factory
from app.models.agent import AgentDefinition, AgentRun
from app.models.conversation import Conversation, ConversationAttachment, Message
from app.models.knowledge import KnowledgeBase
from app.models.user import User
from app.routing import ExecutionPath, get_intent_router
from app.schemas.agent import (
    AgentDefinitionCreate,
    AgentDefinitionResponse,
    AgentDefinitionUpdate,
    AgentRunListItemResponse,
    AgentRunRequest,
    AgentRunResponse,
)

router = APIRouter(tags=["Agents"])

DEFAULT_AGENT_ID = "default-system-agent"
DEFAULT_AGENT_NAME = "企业智能助手 (内置)"


async def get_or_create_default_agent(db: AsyncSession) -> AgentDefinition:
    stmt = select(AgentDefinition).where(AgentDefinition.id == DEFAULT_AGENT_ID)
    agent = (await db.execute(stmt)).scalar_one_or_none()
    if not agent:
        agent = AgentDefinition(
            id=DEFAULT_AGENT_ID,
            name=DEFAULT_AGENT_NAME,
            description="开箱即用的系统默认企业智能知识分析助手，内置知识库检索与自主多步分析决策能力。",
            system_prompt=(
                "你是一个开箱即用的企业级智能知识助手。你具有自主分析、调用知识库检索工具及多步推理的能力。"
                "在回答用户问题时，请优先使用 search_knowledge_base 工具检索客观事实依据，并给出专业、严谨、基于事实的回答。"
            ),
            knowledge_base_id=None,
            enabled_tools=json.dumps(["search_knowledge_base"]),
            max_steps=5,
            max_tool_calls=10,
            timeout_seconds=60.0,
            is_active=True,
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
    return agent


# ==============================================================================
# AgentDefinition CRUD
# ==============================================================================


@router.post("/agents", response_model=AgentDefinitionResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentDefinitionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create a new persistent AgentDefinition with system prompt, tool permissions, and limits.
    """
    # 1. Validate Knowledge Base existence if provided
    if payload.knowledge_base_id:
        kb_stmt = select(KnowledgeBase).where(KnowledgeBase.id == payload.knowledge_base_id)
        if current_user.role != "admin":
            kb_stmt = kb_stmt.where(KnowledgeBase.owner_id == current_user.id)
        kb = (await db.execute(kb_stmt)).scalar_one_or_none()
        if not kb:
            raise KnowledgeBaseNotFoundError(payload.knowledge_base_id)

    # 2. Validate enabled_tools against ToolRegistry
    registry = get_default_tool_registry()
    for tool_name in payload.enabled_tools:
        if not registry.has(tool_name):
            raise InvalidToolConfigurationError(tool_name)

    target_owner_id = None if (payload.is_public and current_user.role == "admin") else current_user.id

    agent = AgentDefinition(
        name=payload.name,
        description=payload.description or "",
        system_prompt=payload.system_prompt,
        knowledge_base_id=payload.knowledge_base_id,
        owner_id=target_owner_id,
        max_steps=payload.max_steps,
        max_tool_calls=payload.max_tool_calls,
        timeout_seconds=payload.timeout_seconds,
        is_active=payload.is_active,
    )
    agent.set_tools(payload.enabled_tools)

    db.add(agent)
    await db.commit()
    await db.refresh(agent)

    return AgentDefinitionResponse(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        system_prompt=agent.system_prompt,
        knowledge_base_id=agent.knowledge_base_id,
        enabled_tools=agent.tools,
        max_steps=agent.max_steps,
        max_tool_calls=agent.max_tool_calls,
        timeout_seconds=agent.timeout_seconds,
        is_active=agent.is_active,
        owner_id=agent.owner_id,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


@router.get("/agents", response_model=List[AgentDefinitionResponse])
async def list_agents(
    is_active: Optional[bool] = Query(default=None, description="Filter by active status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List all configured agents accessible to current user.
    """
    stmt = select(AgentDefinition)
    if current_user.role != "admin":
        stmt = stmt.where((AgentDefinition.owner_id.is_(None)) | (AgentDefinition.owner_id == current_user.id))

    if is_active is not None:
        stmt = stmt.where(AgentDefinition.is_active == is_active)
    stmt = stmt.order_by(desc(AgentDefinition.created_at)).limit(limit).offset(offset)

    agents = (await db.execute(stmt)).scalars().all()
    return [
        AgentDefinitionResponse(
            id=a.id,
            name=a.name,
            description=a.description,
            system_prompt=a.system_prompt,
            knowledge_base_id=a.knowledge_base_id,
            enabled_tools=a.tools,
            max_steps=a.max_steps,
            max_tool_calls=a.max_tool_calls,
            timeout_seconds=a.timeout_seconds,
            is_active=a.is_active,
            owner_id=a.owner_id,
            created_at=a.created_at,
            updated_at=a.updated_at,
        )
        for a in agents
    ]


@router.get("/agents/{agent_id}", response_model=AgentDefinitionResponse)
async def get_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get detailed configuration of a specific agent.
    Supports built-in 'default' / 'default-system-agent'.
    """
    if agent_id in ("default", DEFAULT_AGENT_ID):
        agent = await get_or_create_default_agent(db)
    else:
        stmt = select(AgentDefinition).where(AgentDefinition.id == agent_id)
        agent = (await db.execute(stmt)).scalar_one_or_none()
        if not agent:
            raise AgentDefinitionNotFoundError(agent_id)
        if agent.owner_id is not None and agent.owner_id != current_user.id and current_user.role != "admin":
            raise AgentDefinitionNotFoundError(agent_id)

    return AgentDefinitionResponse(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        system_prompt=agent.system_prompt,
        knowledge_base_id=agent.knowledge_base_id,
        enabled_tools=agent.tools,
        max_steps=agent.max_steps,
        max_tool_calls=agent.max_tool_calls,
        timeout_seconds=agent.timeout_seconds,
        is_active=agent.is_active,
        owner_id=agent.owner_id,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


@router.patch("/agents/{agent_id}", response_model=AgentDefinitionResponse)
async def update_agent(
    agent_id: str,
    payload: AgentDefinitionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Update configuration of an existing agent.
    """
    stmt = select(AgentDefinition).where(AgentDefinition.id == agent_id)
    agent = (await db.execute(stmt)).scalar_one_or_none()
    if not agent:
        raise AgentDefinitionNotFoundError(agent_id)

    if agent.owner_id is None:
        if current_user.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有管理员可以修改系统全局智能体")
    elif agent.owner_id != current_user.id and current_user.role != "admin":
        raise AgentDefinitionNotFoundError(agent_id)

    # Validate KB if updated
    if payload.knowledge_base_id is not None:
        if payload.knowledge_base_id != "":
            kb_stmt = select(KnowledgeBase).where(KnowledgeBase.id == payload.knowledge_base_id)
            if current_user.role != "admin":
                kb_stmt = kb_stmt.where(KnowledgeBase.owner_id == current_user.id)
            kb = (await db.execute(kb_stmt)).scalar_one_or_none()
            if not kb:
                raise KnowledgeBaseNotFoundError(payload.knowledge_base_id)
            agent.knowledge_base_id = payload.knowledge_base_id
        else:
            agent.knowledge_base_id = None

    # Validate tools if updated
    if payload.enabled_tools is not None:
        registry = get_default_tool_registry()
        for t_name in payload.enabled_tools:
            if not registry.has(t_name):
                raise InvalidToolConfigurationError(t_name)
        agent.set_tools(payload.enabled_tools)

    if payload.name is not None:
        agent.name = payload.name
    if payload.description is not None:
        agent.description = payload.description
    if payload.system_prompt is not None:
        agent.system_prompt = payload.system_prompt
    if payload.max_steps is not None:
        agent.max_steps = payload.max_steps
    if payload.max_tool_calls is not None:
        agent.max_tool_calls = payload.max_tool_calls
    if payload.timeout_seconds is not None:
        agent.timeout_seconds = payload.timeout_seconds
    if payload.is_active is not None:
        agent.is_active = payload.is_active

    await db.commit()
    await db.refresh(agent)

    return AgentDefinitionResponse(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        system_prompt=agent.system_prompt,
        knowledge_base_id=agent.knowledge_base_id,
        enabled_tools=agent.tools,
        max_steps=agent.max_steps,
        max_tool_calls=agent.max_tool_calls,
        timeout_seconds=agent.timeout_seconds,
        is_active=agent.is_active,
        owner_id=agent.owner_id,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


@router.delete("/agents/{agent_id}")
async def delete_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Delete an agent definition and cleanly cascade-delete its historical execution runs.
    Guarantees no orphan records remain.
    """
    stmt = select(AgentDefinition).where(AgentDefinition.id == agent_id)
    agent = (await db.execute(stmt)).scalar_one_or_none()
    if not agent:
        raise AgentDefinitionNotFoundError(agent_id)

    if agent.owner_id is None:
        if current_user.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有管理员可以删除系统全局智能体")
    elif agent.owner_id != current_user.id and current_user.role != "admin":
        raise AgentDefinitionNotFoundError(agent_id)

    # Cascade delete all runs within the same transaction to guarantee no orphans
    await db.delete(agent)
    await db.commit()

    return {"message": f"Agent '{agent.name}' (ID: {agent_id}) and all associated execution runs deleted successfully."}


# ==============================================================================
# Agent Execution & Real-time SSE Stream
# ==============================================================================


@router.post("/agents/{agent_id}/runs/stream")
async def run_agent_stream(
    agent_id: str,
    payload: AgentRunRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Execute an Agent Run with real-time Server-Sent Events (SSE).
    Stream lifecycle events:
      run_started -> agent_step_completed -> tool_call_started -> tool_call_completed
      -> citation_verified -> final_answer -> trace -> run_completed (or run_failed / run_cancelled)
    """
    effective_user_id = user_id or settings.DEFAULT_LOCAL_USER_ID

    # 1. Validate AgentDefinition (support built-in default agent)
    if agent_id in ("default", DEFAULT_AGENT_ID):
        agent = await get_or_create_default_agent(db)
    else:
        stmt = select(AgentDefinition).where(AgentDefinition.id == agent_id)
        agent = (await db.execute(stmt)).scalar_one_or_none()
        if not agent:
            raise AgentDefinitionNotFoundError(agent_id)
        if agent.owner_id is not None and agent.owner_id != effective_user_id:
            raise AgentDefinitionNotFoundError(agent_id)

    if not agent.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Agent '{agent.name}' is currently disabled.",
        )

    # 2. Determine target KnowledgeBase (payload takes priority, fallback to conversation, then agent)
    target_kb_id = payload.knowledge_base_id or agent.knowledge_base_id
    if target_kb_id:
        kb_stmt = select(KnowledgeBase).where(
            KnowledgeBase.id == target_kb_id,
            KnowledgeBase.owner_id == effective_user_id,
        )
        kb = (await db.execute(kb_stmt)).scalar_one_or_none()
        if not kb:
            raise KnowledgeBaseNotFoundError(target_kb_id)

    # 3. Resolve or Auto-Create Conversation
    conv = None
    if payload.conversation_id:
        conv_stmt = select(Conversation).where(
            Conversation.id == payload.conversation_id,
            Conversation.owner_id == effective_user_id,
        )
        conv = (await db.execute(conv_stmt)).scalar_one_or_none()
        if not conv:
            raise ConversationNotFoundError(payload.conversation_id)
        if not target_kb_id and conv.knowledge_base_id:
            target_kb_id = conv.knowledge_base_id
    else:
        # Auto-create conversation for new standalone / chat session
        title_text = payload.input[:30] + ("..." if len(payload.input) > 30 else "")
        conv = Conversation(
            knowledge_base_id=target_kb_id,
            title=title_text,
            mode="agentic",
            owner_id=effective_user_id,
        )
        db.add(conv)
        await db.commit()
        await db.refresh(conv)

    conv_id = conv.id if conv else None

    # 3.5. Load completed attachments for current conversation
    completed_attachments: List[ConversationAttachment] = []
    if conv_id:
        att_stmt = (
            select(ConversationAttachment)
            .where(
                ConversationAttachment.conversation_id == conv_id,
                ConversationAttachment.parse_status == "COMPLETED",
            )
            .order_by(ConversationAttachment.created_at.asc())
        )
        completed_attachments = list((await db.execute(att_stmt)).scalars().all())

    # 3.6. Retrieve relevant user memories within budget (P1.2)
    mem_res = await prepare_memory_context(
        user_id=user_id,
        query=payload.input,
        budget=settings.MEMORY_TOKEN_BUDGET,
        db=db,
    )

    # Build system prompt with decision guidance and attachment inventory
    prompt_parts = []
    base_sys_prompt = agent.system_prompt or (
        "你是一个开箱即用的企业级智能知识助手。你具有自主分析、调用知识库检索工具及多步推理的能力。"
        "在回答用户问题时，请优先使用检索工具获取客观事实依据，并给出专业、严谨、基于事实的回答。"
    )
    prompt_parts.append(base_sys_prompt)

    if mem_res.formatted_context:
        prompt_parts.append(f"\n\n{mem_res.formatted_context}")

    decision_guidance = (
        "\n\n【决策与工具调用指引】：\n"
        "1. 当用户的问题涉及当前上传文件的内容、总结、比较、查找或分析时，应优先考虑调用 search_conversation_attachments 工具。\n"
        "2. 当问题涉及企业知识库全局规范、架构设计、产品定义或领域专业知识时，应考虑调用 search_knowledge_base 工具。\n"
        "3. 当用户要求跨来源比较（例如将上传方案与知识库标准对照）时，可以先后调用两个工具分别获取证据。\n"
        "4. 若用户仅进行日常打招呼或提出不需要技术事实依据的常规交流，不必强制调用检索工具。\n"
        "5. 严禁编造未经工具检索证实的事实依据；所有技术结论必须严格依据 [Ref-X] 证据作答。"
    )
    prompt_parts.append(decision_guidance)

    if completed_attachments:
        att_inventory_lines = [
            f"- {a.original_filename} ({a.file_extension.upper().lstrip('.')}, 页数/段落: {a.page_count}, 状态: {a.parse_status})"
            for a in completed_attachments
        ]
        prompt_parts.append(
            "\n\n【当前会话可用附件清单】（请使用 search_conversation_attachments 按需检索，不要凭空臆测）：\n"
            + "\n".join(att_inventory_lines)
        )

    full_system_prompt = "".join(prompt_parts)

    # 4. Multi-turn historical message assembly
    initial_messages: List[BaseMessage] = [SystemMessage(content=full_system_prompt)]
    if conv:
        history_stmt = (
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.created_at.asc())
            .limit(settings.MAX_HISTORY_TURNS * 2)
        )
        history_rows = (await db.execute(history_stmt)).scalars().all()
        for m in history_rows:
            if m.role == "user":
                initial_messages.append(HumanMessage(content=m.content))
            elif m.role == "assistant":
                cleaned_content = (
                    m.content.replace("\n\n[回答已被用户中止]", "")
                    .replace("[回答已被用户中止]", "")
                    .strip()
                )
                if cleaned_content:
                    initial_messages.append(AIMessage(content=cleaned_content))
    initial_messages.append(HumanMessage(content=payload.input))

    # 5. Create initial AgentRun record and commit immediately (avoids long-running transactions)
    run_id = str(uuid.uuid4())
    # Resolve dynamic LLM client and validate Tool Calling capability
    llm_client, model_cfg = await resolve_llm_client(db, payload.model_id, user_id=effective_user_id)
    validate_tool_calling_capability(model_cfg)

    started_at = utc_now()
    agent_run = AgentRun(
        id=run_id,
        agent_definition_id=agent.id,
        conversation_id=conv_id,
        owner_id=effective_user_id,
        status="running",
        input=payload.input,
        started_at=started_at,
    )
    db.add(agent_run)
    await db.commit()

    # Pre-configure runtime parameters from AgentDefinition
    tool_registry = get_default_tool_registry()
    kb_id = target_kb_id
    enabled_tools = list(agent.tools)
    if completed_attachments and "search_conversation_attachments" not in enabled_tools:
        enabled_tools.append("search_conversation_attachments")
    max_steps = agent.max_steps
    max_tool_calls = agent.max_tool_calls
    timeout_sec = agent.timeout_seconds

    # Intent routing for Agent mode
    router = get_intent_router()
    history_message_dicts: List[Dict[str, str]] = []
    if conv:
        for m in (history_rows if "history_rows" in locals() else []):
            if m.role in ("user", "assistant"):
                cleaned = m.content.replace("\n\n[回答已被用户中止]", "").replace("[回答已被用户中止]", "").strip()
                if cleaned:
                    history_message_dicts.append({"role": m.role, "content": cleaned})

    decision = router.route(
        query=payload.input,
        has_knowledge_base=bool(target_kb_id),
        has_completed_attachments=bool(completed_attachments),
        history_messages=history_message_dicts,
        requested_mode="agentic",
    )

    async def sse_generator():
        start_t = time.perf_counter()

        # If routed to DIRECT_LLM, bypass LangGraph graph and tool calling completely
        if decision.execution_path == ExecutionPath.DIRECT_LLM:
            logger.info(f"Agent run {run_id} routed to DIRECT_LLM (intent: {decision.intent.value}, reason: {decision.reason})")
            yield f"event: run_started\ndata: {json.dumps({'run_id': run_id, 'conversation_id': conv_id, 'query': payload.input, 'execution_path': 'DIRECT_LLM', 'intent': decision.intent.value, 'reason': decision.reason, 'enabled_tools': []}, ensure_ascii=False)}\n\n"

            accumulated = ""
            gen_start = time.perf_counter()
            ttft_ms = None

            hist_lines = []
            for hm in history_message_dicts[-6:]:
                role_label = "用户" if hm["role"] == "user" else "AI助手"
                hist_lines.append(f"{role_label}: {hm['content']}")
            if hist_lines:
                direct_prompt = "【前序对话历史】（仅供理解上下文背景）：\n" + "\n".join(hist_lines) + f"\n\n【用户当前提问】：\n{payload.input}"
            else:
                direct_prompt = payload.input

            if mem_res.formatted_context:
                direct_prompt = f"{mem_res.formatted_context}\n\n{direct_prompt}"

            direct_sys_prompt = (
                "你是由 AgentStudio 驱动的企业级智能 AI 助手。\n"
                "请根据自身广泛的通用知识和专业能力，清晰、严谨、客观、准确地回答用户的问题。\n"
                "排版请使用结构良好、清晰易读的 Markdown 格式。"
            )

            try:
                async for token in llm_client.generate_stream(
                    prompt=direct_prompt,
                    system_prompt=direct_sys_prompt,
                ):
                    if ttft_ms is None:
                        ttft_ms = round((time.perf_counter() - gen_start) * 1000.0, 2)
                    accumulated += token
                    yield f"event: final_answer\ndata: {json.dumps({'delta': token, 'answer': accumulated}, ensure_ascii=False)}\n\n"
            except asyncio.CancelledError:
                if accumulated.strip():
                    accumulated += "\n\n[回答已被用户中止]"
                else:
                    accumulated = "[回答已被用户中止]"
                elapsed_ms = round((time.perf_counter() - start_t) * 1000.0, 2)
                agent_run.status = "cancelled"
                agent_run.final_answer = accumulated
                agent_run.completed_at = utc_now()
                agent_run.duration_ms = elapsed_ms
                agent_run.execution_steps = 1
                agent_run.tool_call_count = 0
                db.add(agent_run)
                if conv_id:
                    user_msg = Message(
                        conversation_id=conv_id,
                        role="user",
                        content=payload.input,
                    )
                    asst_msg = Message(
                        conversation_id=conv_id,
                        role="assistant",
                        content=accumulated,
                    )
                    asst_msg.execution_info = {
                        "mode": "agentic",
                        "execution_path": "DIRECT_LLM",
                        "intent": decision.intent.value,
                        "route_reason": decision.reason,
                        "total_duration_ms": elapsed_ms,
                        "tool_calls_count": 0,
                        "execution_steps": 1,
                        "llm_model": model_cfg.model_name,
                        "memory_used": mem_res.used_memory_tokens > 0,
                        "retrieved_memory_count": mem_res.retrieved_count,
                        "injected_memory_count": mem_res.injected_count,
                        "memory_tokens": mem_res.used_memory_tokens,
                        "memory_retrieval_duration_ms": mem_res.duration_ms,
                    }
                    db.add_all([user_msg, asst_msg])
                try:
                    await db.commit()
                except Exception:
                    pass
                raise

            total_ms = round((time.perf_counter() - start_t) * 1000.0, 2)
            yield f"event: citation_verified\ndata: {json.dumps({'citations': [], 'count': 0}, ensure_ascii=False)}\n\n"
            exec_info_data = {
                "run_id": run_id,
                "mode": "agentic",
                "execution_path": "DIRECT_LLM",
                "intent": decision.intent.value,
                "route_reason": decision.reason,
                "tool_calls_count": 0,
                "execution_steps": 1,
                "total_duration_ms": total_ms,
                "llm_model": model_cfg.model_name,
                "ttft_ms": ttft_ms,
                "memory_used": mem_res.used_memory_tokens > 0,
                "retrieved_memory_count": mem_res.retrieved_count,
                "injected_memory_count": mem_res.injected_count,
                "memory_tokens": mem_res.used_memory_tokens,
                "memory_retrieval_duration_ms": mem_res.duration_ms,
            }
            yield f"event: trace\ndata: {json.dumps(exec_info_data, ensure_ascii=False)}\n\n"
            yield f"event: run_completed\ndata: {json.dumps({'run_id': run_id, 'status': 'completed', 'final_answer': accumulated, 'citations': [], 'execution_steps': 1, 'tool_calls_count': 0, 'total_duration_ms': total_ms}, ensure_ascii=False)}\n\n"

            agent_run.status = "completed"
            agent_run.final_answer = accumulated
            agent_run.completed_at = utc_now()
            agent_run.duration_ms = total_ms
            agent_run.execution_steps = 1
            agent_run.tool_call_count = 0
            db.add(agent_run)

            if conv_id:
                user_msg = Message(
                    conversation_id=conv_id,
                    role="user",
                    content=payload.input,
                )
                asst_msg = Message(
                    conversation_id=conv_id,
                    role="assistant",
                    content=accumulated,
                )
                asst_msg.execution_info = exec_info_data
                db.add_all([user_msg, asst_msg])
                if conv:
                    conv.updated_at = utc_now()
            await db.commit()

            if conv_id and asst_msg:
                asyncio.create_task(
                    run_memory_extraction_task(
                        user_id=effective_user_id,
                        query=payload.input,
                        answer=accumulated,
                        conversation_id=conv_id,
                        message_id=asst_msg.id,
                    )
                )
            return

        final_answer_text = None
        final_status = "completed"
        err_msg = None
        citations_data = []
        traces_data = []
        steps_count = 0
        calls_count = 0

        # Timeline state trackers for visualization in unified chat workspace
        timeline_steps = []
        timeline_tool_calls = {}

        # Construct isolated runtime
        runtime = LangGraphAgentRuntime(
            tool_registry=tool_registry,
            llm_client=llm_client,
            db_session=db,
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
            tool_timeout=min(30.0, timeout_sec),
            run_timeout=timeout_sec,
        )

        try:
            async for evt in runtime.astream_events(
                query=payload.input,
                knowledge_base_id=kb_id,
                conversation_id=conv_id,
                attachment_ids=[a.id for a in completed_attachments],
                enabled_tools=enabled_tools,
                run_id=run_id,
                initial_messages=initial_messages,
                memory_context=mem_res.formatted_context,
                memory_info={
                    "memory_used": mem_res.used_memory_tokens > 0,
                    "retrieved_memory_count": mem_res.retrieved_count,
                    "injected_memory_count": mem_res.injected_count,
                    "memory_tokens": mem_res.used_memory_tokens,
                    "memory_retrieval_duration_ms": mem_res.duration_ms,
                },
                user_id=effective_user_id,
            ):
                evt_name = evt.get("event")
                evt_data = evt.get("data", {})

                # Augment event with conversation_id for seamless client state tracking
                if conv_id and "conversation_id" not in evt_data:
                    evt_data["conversation_id"] = conv_id

                # Track key state updates for persistence and timeline
                if evt_name == "agent_step_completed":
                    st = evt_data.get("step")
                    if isinstance(st, int):
                        steps_count = max(steps_count, st)
                        timeline_steps.append({
                            "step": st,
                            "decision": evt_data.get("decision", ""),
                            "tools": evt_data.get("tools", []),
                            "tool_calls_count": evt_data.get("tool_calls_count", 0),
                            "timestamp": evt_data.get("timestamp"),
                        })
                elif evt_name == "tool_call_started":
                    tc_id = evt_data.get("tool_call_id")
                    if tc_id:
                        timeline_tool_calls[tc_id] = {
                            "tool_call_id": tc_id,
                            "tool_name": evt_data.get("tool_name"),
                            "args": evt_data.get("args"),
                            "status": "calling",
                            "timestamp": evt_data.get("timestamp"),
                        }
                elif evt_name == "tool_call_completed":
                    calls_count += 1
                    tc_id = evt_data.get("tool_call_id")
                    if tc_id:
                        if tc_id not in timeline_tool_calls:
                            timeline_tool_calls[tc_id] = {
                                "tool_call_id": tc_id,
                                "tool_name": evt_data.get("tool_name"),
                            }
                        timeline_tool_calls[tc_id].update({
                            "status": "success" if evt_data.get("success") else "failed",
                            "duration_ms": evt_data.get("duration_ms"),
                            "evidence_count": evt_data.get("evidence_count"),
                            "observation_preview": evt_data.get("observation_preview"),
                            "error": evt_data.get("error"),
                        })
                elif evt_name == "citation_verified":
                    citations_data = evt_data.get("citations", [])
                elif evt_name == "final_answer":
                    final_answer_text = evt_data.get("final_answer")
                elif evt_name == "trace":
                    traces_data = evt_data.get("trace_events", [])
                    evt_data["memory_used"] = mem_res.used_memory_tokens > 0
                    evt_data["retrieved_memory_count"] = mem_res.retrieved_count
                    evt_data["injected_memory_count"] = mem_res.injected_count
                    evt_data["memory_tokens"] = mem_res.used_memory_tokens
                    evt_data["memory_retrieval_duration_ms"] = mem_res.duration_ms
                elif evt_name == "run_completed":
                    final_status = evt_data.get("status", "completed")
                    evt_data["memory_used"] = mem_res.used_memory_tokens > 0
                    evt_data["injected_memory_count"] = mem_res.injected_count
                    evt_data["memory_tokens"] = mem_res.used_memory_tokens
                elif evt_name == "run_failed":
                    final_status = "failed"
                    err_msg = evt_data.get("error")

                # Format SSE chunk
                data_json = json.dumps(evt_data, ensure_ascii=False)
                yield f"event: {evt_name}\ndata: {data_json}\n\n"

        except asyncio.CancelledError:
            # Client disconnected / aborted
            final_status = "cancelled"
            elapsed_ms = round((time.perf_counter() - start_t) * 1000, 2)
            try:
                agent_run.status = "cancelled"
                agent_run.completed_at = utc_now()
                agent_run.duration_ms = elapsed_ms
                agent_run.execution_steps = steps_count
                agent_run.tool_call_count = calls_count
                agent_run.execution_trace_json = json.dumps(traces_data, ensure_ascii=False)
                db.add(agent_run)

                # Persist partial cancelled message
                if conv_id:
                    msg_user = Message(
                        conversation_id=conv_id,
                        role="user",
                        content=payload.input,
                    )
                    cancelled_answer = (final_answer_text or "") + "\n\n[回答已被用户中止]"
                    msg_assistant = Message(
                        conversation_id=conv_id,
                        role="assistant",
                        content=cancelled_answer,
                        citations_json=json.dumps(citations_data, ensure_ascii=False),
                        execution_info_json=json.dumps({
                            "mode": "agentic",
                            "run_id": run_id,
                            "duration_ms": elapsed_ms,
                            "steps": steps_count,
                            "tool_calls": calls_count,
                            "status": "cancelled",
                        }, ensure_ascii=False),
                    )
                    db.add_all([msg_user, msg_assistant])
                await db.commit()
            except Exception:
                pass
            raise

        except Exception as exc:
            final_status = "failed"
            err_msg = str(exc)
            elapsed_ms = round((time.perf_counter() - start_t) * 1000, 2)
            err_payload = json.dumps({"run_id": run_id, "conversation_id": conv_id, "error": str(exc)}, ensure_ascii=False)
            yield f"event: run_failed\ndata: {err_payload}\n\n"

        finally:
            if final_status != "cancelled":
                elapsed_ms = round((time.perf_counter() - start_t) * 1000, 2)
                try:
                    agent_run.status = final_status
                    agent_run.final_answer = final_answer_text
                    agent_run.error = err_msg
                    agent_run.citations_json = json.dumps(citations_data, ensure_ascii=False)
                    agent_run.execution_trace_json = json.dumps(traces_data, ensure_ascii=False)
                    agent_run.execution_steps = steps_count
                    agent_run.tool_call_count = calls_count
                    agent_run.completed_at = utc_now()
                    agent_run.duration_ms = elapsed_ms
                    db.add(agent_run)

                    if conv_id and final_status in ["completed", "max_steps_reached", "failed"]:
                        conv_stmt = select(Conversation).where(Conversation.id == conv_id)
                        conv_obj = (await db.execute(conv_stmt)).scalar_one_or_none()
                        if conv_obj:
                            msg_user = Message(
                                conversation_id=conv_obj.id,
                                role="user",
                                content=payload.input,
                            )
                            agent_timeline_summary = {
                                "run_id": run_id,
                                "status": final_status,
                                "duration_ms": elapsed_ms,
                                "steps": timeline_steps,
                                "tool_calls": list(timeline_tool_calls.values()),
                                "citations_count": len(citations_data),
                                "error": err_msg,
                            }
                            msg_assistant = Message(
                                conversation_id=conv_obj.id,
                                role="assistant",
                                content=final_answer_text or (f"执行错误: {err_msg}" if err_msg else ""),
                                citations_json=json.dumps(citations_data, ensure_ascii=False),
                                execution_info_json=json.dumps({
                                    "mode": "agentic",
                                    "run_id": run_id,
                                    "duration_ms": elapsed_ms,
                                    "steps": steps_count,
                                    "tool_calls": calls_count,
                                    "agent_timeline": agent_timeline_summary,
                                    "status": final_status,
                                    "memory_used": mem_res.used_memory_tokens > 0,
                                    "retrieved_memory_count": mem_res.retrieved_count,
                                    "injected_memory_count": mem_res.injected_count,
                                    "memory_tokens": mem_res.used_memory_tokens,
                                    "memory_retrieval_duration_ms": mem_res.duration_ms,
                                }, ensure_ascii=False),
                            )
                            db.add_all([msg_user, msg_assistant])
                            conv_obj.updated_at = utc_now()
                    await db.commit()

                    if conv_id and final_status == "completed" and not err_msg and 'msg_assistant' in locals():
                        asyncio.create_task(
                            run_memory_extraction_task(
                                user_id=effective_user_id,
                                query=payload.input,
                                answer=final_answer_text or "",
                                conversation_id=conv_id,
                                message_id=msg_assistant.id,
                            )
                        )
                except Exception as save_err:
                    logger.error(f"Failed to persist terminal state for AgentRun '{run_id}': {save_err}", exc_info=True)

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ==============================================================================
# Historical Runs Query APIs
# ==============================================================================


@router.get("/agents/{agent_id}/runs", response_model=List[AgentRunListItemResponse])
async def list_agent_runs(
    agent_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    List historical execution runs for a specific agent belonging to current user.
    """
    # Verify agent exists and is accessible
    a_stmt = select(AgentDefinition).where(AgentDefinition.id == agent_id)
    agent = (await db.execute(a_stmt)).scalar_one_or_none()
    if not agent:
        raise AgentDefinitionNotFoundError(agent_id)
    if agent.owner_id is not None and agent.owner_id != user_id:
        raise AgentDefinitionNotFoundError(agent_id)

    stmt = (
        select(AgentRun)
        .where(
            AgentRun.agent_definition_id == agent_id,
            AgentRun.owner_id == user_id,
        )
        .order_by(desc(AgentRun.created_at))
        .limit(limit)
        .offset(offset)
    )
    runs = (await db.execute(stmt)).scalars().all()

    return [
        AgentRunListItemResponse(
            id=r.id,
            agent_definition_id=r.agent_definition_id,
            conversation_id=r.conversation_id,
            status=r.status,
            input=r.input,
            final_answer_preview=(r.final_answer or "")[:120] if r.final_answer else None,
            execution_steps=r.execution_steps,
            tool_call_count=r.tool_call_count,
            citations_count=len(r.citations),
            owner_id=r.owner_id,
            started_at=r.started_at,
            completed_at=r.completed_at,
            duration_ms=r.duration_ms,
            created_at=r.created_at,
        )
        for r in runs
    ]


@router.get("/agent-runs/{run_id}", response_model=AgentRunResponse)
async def get_agent_run_detail(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """
    Get full execution details, citations, and observable trace of a specific Agent Run.
    Enforces tenant isolation (404 on mismatch).
    """
    stmt = select(AgentRun).where(
        AgentRun.id == run_id,
        AgentRun.owner_id == user_id,
    )
    run_obj = (await db.execute(stmt)).scalar_one_or_none()
    if not run_obj:
        raise AgentRunNotFoundError(run_id)

    return AgentRunResponse(
        id=run_obj.id,
        agent_definition_id=run_obj.agent_definition_id,
        conversation_id=run_obj.conversation_id,
        status=run_obj.status,
        input=run_obj.input,
        final_answer=run_obj.final_answer,
        error=run_obj.error,
        execution_steps=run_obj.execution_steps,
        tool_call_count=run_obj.tool_call_count,
        citations=run_obj.citations,
        execution_trace=run_obj.execution_trace,
        owner_id=run_obj.owner_id,
        started_at=run_obj.started_at,
        completed_at=run_obj.completed_at,
        duration_ms=run_obj.duration_ms,
        created_at=run_obj.created_at,
    )
