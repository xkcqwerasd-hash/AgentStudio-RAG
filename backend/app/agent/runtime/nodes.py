import asyncio
import json
import time
from typing import Any, Dict, List, Optional
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig

from app.agent.runtime.state import AgentState
from app.agent.tools.evidence import EvidenceRegistry
from app.agent.tools.registry import ToolRegistry
from app.agent.tools.schemas import ToolResult
from app.core.logging import logger
from app.llm.base import BaseLLMClient
from app.rag.citation import CitationExtractor


async def agent_node(state: AgentState, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    """
    Agent Node:
    1. Loads current conversation history & observations.
    2. Filters allowed tools based on state["enabled_tools"].
    3. Calls LLM with standard Function Calling / Tool Calling JSON schema.
    4. Records observable decision traces without leaking internal CoT thoughts.
    5. Updates execution step counter.
    """
    configurable = (config or {}).get("configurable", {})
    llm_client: BaseLLMClient = configurable["llm_client"]
    tool_registry: ToolRegistry = configurable["tool_registry"]

    curr_steps = state.get("execution_steps", 0)
    curr_tool_calls = state.get("tool_call_count", 0)
    max_steps = configurable.get("max_steps", 5)
    max_tool_calls = configurable.get("max_tool_calls", 10)

    # Filter enabled tools schema
    enabled_tools = state.get("enabled_tools", [])
    all_tool_schemas = tool_registry.to_openai_tools()
    allowed_tools = [
        t for t in all_tool_schemas if t.get("function", {}).get("name") in enabled_tools
    ]

    # If steps or calls already reached limit, do not pass tools
    tools_param = allowed_tools if (curr_steps < max_steps and curr_tool_calls < max_tool_calls) else None

    start_t = time.perf_counter()
    try:
        ai_message = await llm_client.generate_with_tools(
            messages=list(state["messages"]),
            tools=tools_param,
            tool_choice="auto" if tools_param else "none",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(f"Agent Node LLM generation error: {exc}", exc_info=True)
        # Create an error trace and fail safely
        err_msg = f"LLM generation failed: {str(exc)}"
        trace = {
            "event": "agent_llm_error",
            "step": curr_steps + 1,
            "error": err_msg,
            "timestamp": time.time(),
        }
        return {
            "execution_steps": curr_steps + 1,
            "status": "failed",
            "error": err_msg,
            "trace_events": state.get("trace_events", []) + [trace],
        }

    duration_ms = round((time.perf_counter() - start_t) * 1000, 2)
    next_step = curr_steps + 1

    tool_calls = getattr(ai_message, "tool_calls", []) or []
    if tool_calls:
        trace = {
            "event": "agent_tool_decision",
            "step": next_step,
            "tool_calls_count": len(tool_calls),
            "tools": [tc.get("name") for tc in tool_calls],
            "duration_ms": duration_ms,
            "timestamp": time.time(),
        }
    else:
        trace = {
            "event": "agent_response_generated",
            "step": next_step,
            "content_preview": (ai_message.content or "")[:120],
            "duration_ms": duration_ms,
            "timestamp": time.time(),
        }

    return {
        "messages": [ai_message],
        "execution_steps": next_step,
        "trace_events": state.get("trace_events", []) + [trace],
    }


async def tool_node(state: AgentState, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    """
    Tool Node:
    1. Extracts tool_calls from latest AIMessage.
    2. Enforces security boundaries: tool whitelist and trusted knowledge_base_id.
    3. Executes tools via ToolRegistry with execution timeout and cancellation safety.
    4. Synchronizes evidence with Run-scoped EvidenceRegistry.
    5. Returns standard ToolMessages to feed back into the Agent loop.
    """
    configurable = (config or {}).get("configurable", {})
    tool_registry: ToolRegistry = configurable["tool_registry"]
    evidence_registry: EvidenceRegistry = configurable["evidence_registry"]
    db_session = configurable.get("db_session")
    tool_timeout: float = configurable.get("tool_timeout", 30.0)

    last_message = state["messages"][-1] if state.get("messages") else None
    tool_calls = getattr(last_message, "tool_calls", []) or []

    enabled_tools = set(state.get("enabled_tools", []))
    trusted_kb_id = state.get("knowledge_base_id")

    tool_messages: List[ToolMessage] = []
    traces: List[Dict[str, Any]] = list(state.get("trace_events", []))
    curr_tool_call_count = state.get("tool_call_count", 0)

    for tc in tool_calls:
        tc_id = tc.get("id") or f"call_{time.time_ns()}"
        tool_name = tc.get("name", "")
        raw_args = tc.get("args", {})
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except Exception as j_err:
                err_msg = f"Tool arguments JSON parse error: {j_err}"
                tool_messages.append(
                    ToolMessage(
                        content=json.dumps({"error": err_msg}, ensure_ascii=False),
                        tool_call_id=tc_id,
                        name=tool_name,
                    )
                )
                curr_tool_call_count += 1
                continue

        # Security Check 1: Tool permission whitelist
        if tool_name not in enabled_tools:
            err_msg = f"Security rejection: Tool '{tool_name}' is not permitted for this run."
            logger.warning(err_msg)
            tool_messages.append(
                ToolMessage(
                    content=json.dumps({"error": err_msg}, ensure_ascii=False),
                    tool_call_id=tc_id,
                    name=tool_name,
                )
            )
            curr_tool_call_count += 1
            traces.append({
                "event": "tool_security_rejection",
                "tool_name": tool_name,
                "tool_call_id": tc_id,
                "error": err_msg,
                "timestamp": time.time(),
            })
            continue

        # Security Check 2: Enforce trusted runtime boundaries
        if tool_name == "search_knowledge_base" and trusted_kb_id:
            raw_args["knowledge_base_id"] = trusted_kb_id

        trusted_conv_id = state.get("conversation_id")

        # Tool discovery
        tool_instance = tool_registry.get(tool_name)
        if not tool_instance:
            err_msg = f"Tool '{tool_name}' is not registered in ToolRegistry."
            tool_messages.append(
                ToolMessage(
                    content=json.dumps({"error": err_msg}, ensure_ascii=False),
                    tool_call_id=tc_id,
                    name=tool_name,
                )
            )
            curr_tool_call_count += 1
            continue

        # Attach Run-scoped dependencies if supported
        if hasattr(tool_instance, "with_evidence_registry"):
            tool_instance = tool_instance.with_evidence_registry(evidence_registry)
        if hasattr(tool_instance, "with_session") and db_session is not None:
            tool_instance = tool_instance.with_session(db_session)
        if hasattr(tool_instance, "with_conversation_id") and trusted_conv_id:
            tool_instance = tool_instance.with_conversation_id(trusted_conv_id)
        if hasattr(tool_instance, "with_user_id") and state.get("user_id"):
            tool_instance = tool_instance.with_user_id(state["user_id"])

        # Timed execution with cancellation preservation
        start_t = time.perf_counter()
        try:
            tool_result: ToolResult = await asyncio.wait_for(
                tool_instance.run(**raw_args),
                timeout=tool_timeout,
            )
        except asyncio.CancelledError:
            # Cancellation must propagate directly!
            logger.info(f"Tool execution for '{tool_name}' cancelled by caller.")
            raise
        except asyncio.TimeoutError:
            duration_ms = round((time.perf_counter() - start_t) * 1000, 2)
            err_msg = f"Tool '{tool_name}' execution timed out after {tool_timeout}s."
            logger.error(err_msg)
            tool_result = ToolResult(
                tool_name=tool_name,
                success=False,
                error=err_msg,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            duration_ms = round((time.perf_counter() - start_t) * 1000, 2)
            err_msg = f"Tool '{tool_name}' raised unexpected exception: {exc}"
            logger.error(err_msg, exc_info=True)
            tool_result = ToolResult(
                tool_name=tool_name,
                success=False,
                error=err_msg,
                duration_ms=duration_ms,
            )

        # Format ToolMessage
        msg_content = tool_result.to_tool_message_content()
        tool_messages.append(
            ToolMessage(
                content=msg_content,
                tool_call_id=tc_id,
                name=tool_name,
            )
        )
        curr_tool_call_count += 1

        res_data = tool_result.data if isinstance(tool_result.data, dict) else {}
        source_type = res_data.get("source_type") or (
            "knowledge_base" if "knowledge_base" in tool_name else (
                "attachment" if "attachment" in tool_name else "other"
            )
        )
        traces.append({
            "event": "tool_executed",
            "tool_name": tool_name,
            "tool_call_id": tc_id,
            "success": tool_result.success,
            "duration_ms": tool_result.duration_ms,
            "error": tool_result.error,
            "source_type": source_type,
            "attachment_id": res_data.get("attachment_id"),
            "original_filename": res_data.get("original_filename"),
            "retrieval_mode": res_data.get("strategy") or ("hybrid" if "knowledge_base" in tool_name else None),
            "retrieved_chunks": res_data.get("retrieved_chunks", res_data.get("evidence_count", 0)),
            "returned_chunks": res_data.get("returned_chunks", res_data.get("evidence_count", 0)),
            "retrieval_duration_ms": res_data.get("total_retrieval_duration_ms", tool_result.duration_ms),
            "evidence_refs": res_data.get("evidence_refs", []),
            "evidence_tokens": res_data.get("used_tokens"),
            "timestamp": time.time(),
        })

    # Sync Run-scoped evidence
    updated_evidence = [e.model_dump() for e in evidence_registry.get_all_evidence_items()]

    return {
        "messages": tool_messages,
        "tool_call_count": curr_tool_call_count,
        "evidence": updated_evidence,
        "trace_events": traces,
    }


async def force_stop_node(state: AgentState, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    """
    Force Stop Node:
    When step limit or tool call limit is reached, gracefully instructs the LLM
    to generate the best possible final answer using already collected evidence,
    strictly prohibiting further tool calls.
    """
    configurable = (config or {}).get("configurable", {})
    llm_client: BaseLLMClient = configurable["llm_client"]

    stop_instruction = SystemMessage(
        content=(
            "系统提示：当前任务的执行步数或工具调用次数已达上限。"
            "请严格基于目前已获取的检索事实证据给出最佳回答，禁止继续调用任何工具。"
        )
    )
    prompt_messages = list(state.get("messages", [])) + [stop_instruction]

    try:
        final_ai = await llm_client.generate_with_tools(
            messages=prompt_messages,
            tools=None,
            tool_choice="none",
        )
        if not (final_ai.content and final_ai.content.strip()):
            final_ai = AIMessage(content="系统已达到最大步数限制，依据现有检索结果完成总结。")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(f"Force stop LLM generation error: {exc}", exc_info=True)
        final_ai = AIMessage(content="由于执行限制且模型总结发生异常，无法生成最终回答。")

    trace = {
        "event": "agent_forced_final_summary",
        "reason": "Execution step or tool call limit reached",
        "content_preview": (final_ai.content or "")[:120],
        "timestamp": time.time(),
    }

    return {
        "messages": [final_ai],
        "status": "max_steps_reached",
        "trace_events": state.get("trace_events", []) + [trace],
    }


async def citation_verifier_node(state: AgentState, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    """
    Citation Verifier Node:
    1. Extracts the final answer text from the latest AIMessage.
    2. Retrieves all verified chunks from the Run-scoped EvidenceRegistry.
    3. Runs CitationExtractor to filter fabricated citations and align with [Ref-X].
    4. Sets final_answer, citations, and completed status.
    """
    configurable = (config or {}).get("configurable", {})
    evidence_registry: EvidenceRegistry = configurable["evidence_registry"]

    last_message = state["messages"][-1] if state.get("messages") else None
    answer_text = (getattr(last_message, "content", "") or "") if last_message else ""

    all_chunks = evidence_registry.get_all_chunks()
    verified_citations = CitationExtractor.extract_citations(answer_text, all_chunks)
    citations_data = [c.model_dump() for c in verified_citations]

    curr_status = state.get("status", "running")
    if curr_status in ["failed", "max_steps_reached", "timeout", "cancelled"]:
        final_status = curr_status
    else:
        final_status = "completed"

    trace = {
        "event": "citations_verified",
        "citations_count": len(citations_data),
        "cited_ref_indices": [c["citation_index"] for c in citations_data],
        "timestamp": time.time(),
    }

    return {
        "final_answer": answer_text,
        "citations": citations_data,
        "status": final_status,
        "trace_events": state.get("trace_events", []) + [trace],
    }
