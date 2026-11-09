import asyncio
import json
import time
import uuid
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
from app.agent.tools.schemas import ToolResult, clean_and_parse_json
from app.core.logging import logger
from app.llm.base import BaseLLMClient
from app.rag.citation import CitationExtractor


def compute_tool_fingerprint(tool_name: str, args: Any) -> str:
    """
    Compute a deterministic canonical fingerprint for a tool call.
    1. Handles dict, stringified JSON, or raw arguments (including markdown codeblocks).
    2. Sorts dict keys, strips string whitespace, and ignores None values.
    """
    if isinstance(args, str):
        try:
            args = clean_and_parse_json(args)
        except Exception:
            return f"{tool_name}:{args.strip()}"

    if not isinstance(args, dict):
        return f"{tool_name}:{str(args)}"

    clean_args = {
        k: (v.strip() if isinstance(v, str) else v)
        for k, v in args.items()
        if v is not None
    }
    canonical_json = json.dumps(clean_args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"{tool_name}:{canonical_json}"


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
    curr_param_errors = state.get("consecutive_param_errors", 0)
    curr_duplicate_calls = state.get("consecutive_duplicate_calls", 0)
    curr_json_errors = state.get("consecutive_json_errors", 0)
    max_steps = configurable.get("max_steps", 5)
    max_tool_calls = configurable.get("max_tool_calls", 10)
    max_consecutive_param_errors = configurable.get("max_consecutive_param_errors", 2)
    max_consecutive_duplicate_calls = configurable.get("max_consecutive_duplicate_calls", 2)
    max_consecutive_json_errors = configurable.get("max_consecutive_json_errors", 2)

    # Filter enabled tools schema
    enabled_tools = state.get("enabled_tools", [])
    all_tool_schemas = tool_registry.to_openai_tools()
    allowed_tools = [
        t for t in all_tool_schemas if t.get("function", {}).get("name") in enabled_tools
    ]

    # If steps, calls, param errors, duplicate calls, or json errors reached limit, do not pass tools
    tools_allowed = (
        curr_steps < max_steps
        and curr_tool_calls < max_tool_calls
        and curr_param_errors < max_consecutive_param_errors
        and curr_duplicate_calls < max_consecutive_duplicate_calls
        and curr_json_errors < max_consecutive_json_errors
    )
    tools_param = allowed_tools if tools_allowed else None

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
    invalid_tool_calls = getattr(ai_message, "invalid_tool_calls", []) or []
    all_calls = tool_calls + invalid_tool_calls
    if all_calls:
        trace = {
            "event": "agent_tool_decision",
            "step": next_step,
            "tool_calls_count": len(all_calls),
            "tools": [tc.get("name") for tc in all_calls],
            "invalid_tool_calls_count": len(invalid_tool_calls),
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
    max_consecutive_param_errors: int = configurable.get("max_consecutive_param_errors", 2)
    max_consecutive_duplicate_calls: int = configurable.get("max_consecutive_duplicate_calls", 2)
    max_consecutive_json_errors: int = configurable.get("max_consecutive_json_errors", 2)

    curr_consecutive_param_errors: int = state.get("consecutive_param_errors", 0)
    curr_consecutive_duplicate_calls: int = state.get("consecutive_duplicate_calls", 0)
    curr_consecutive_json_errors: int = state.get("consecutive_json_errors", 0)
    executed_fps: List[str] = list(state.get("executed_tool_fingerprints", []))

    last_message = state["messages"][-1] if state.get("messages") else None
    tool_calls = getattr(last_message, "tool_calls", []) or []
    invalid_tool_calls = getattr(last_message, "invalid_tool_calls", []) or []

    enabled_tools = set(state.get("enabled_tools", []))
    trusted_kb_id = state.get("knowledge_base_id")

    tool_messages: List[ToolMessage] = []
    traces: List[Dict[str, Any]] = list(state.get("trace_events", []))
    curr_tool_call_count = state.get("tool_call_count", 0)

    # 1. Process valid tool_calls
    for tc in tool_calls:
        tc_id = tc.get("id")
        if not tc_id:
            tc_id = f"call_{uuid.uuid4().hex[:8]}"
            tc["id"] = tc_id
        tool_name = tc.get("name", "")
        raw_args = tc.get("args", {})

        # P0-1, P0-2 & P0-3 Guard: If limits reached, block remaining
        if (
            curr_consecutive_param_errors >= max_consecutive_param_errors
            or curr_consecutive_duplicate_calls >= max_consecutive_duplicate_calls
            or curr_consecutive_json_errors >= max_consecutive_json_errors
        ):
            if curr_consecutive_param_errors >= max_consecutive_param_errors:
                reason_msg = "consecutive parameter error limit"
                limit_val = max_consecutive_param_errors
            elif curr_consecutive_duplicate_calls >= max_consecutive_duplicate_calls:
                reason_msg = "consecutive duplicate call limit"
                limit_val = max_consecutive_duplicate_calls
            else:
                reason_msg = "consecutive JSON parse error limit"
                limit_val = max_consecutive_json_errors

            err_msg = (
                f"Execution blocked: Tool '{tool_name}' was not executed because the {reason_msg} ({limit_val}) has been reached."
            )
            logger.warning(err_msg)
            tool_messages.append(
                ToolMessage(
                    content=json.dumps({"status": "blocked", "tool_name": tool_name, "error": err_msg}, ensure_ascii=False),
                    tool_call_id=tc_id,
                    name=tool_name,
                )
            )
            curr_tool_call_count += 1
            traces.append({
                "event": "tool_execution_blocked",
                "tool_name": tool_name,
                "tool_call_id": tc_id,
                "reason": f"{reason_msg}_reached",
                "error": err_msg,
                "timestamp": time.time(),
            })
            continue

        if isinstance(raw_args, str):
            try:
                raw_args = clean_and_parse_json(raw_args)
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
                curr_consecutive_json_errors += 1
                traces.append({
                    "event": "tool_arguments_json_error",
                    "tool_name": tool_name,
                    "tool_call_id": tc_id,
                    "error": err_msg,
                    "timestamp": time.time(),
                })
                continue

        # If arguments parsed successfully and no invalid_tool_calls in this turn, reset JSON error counter
        if not invalid_tool_calls:
            curr_consecutive_json_errors = 0

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
            traces.append({
                "event": "tool_not_registered",
                "tool_name": tool_name,
                "tool_call_id": tc_id,
                "error": err_msg,
                "timestamp": time.time(),
            })
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

        # P0-2: Duplicate Call Detection & Anti-Oscillation
        fp = compute_tool_fingerprint(tool_name, raw_args)
        if fp in executed_fps:
            curr_consecutive_duplicate_calls += 1
            err_msg = (
                f"检测到重复工具调用：工具 '{tool_name}' 已在当前任务中使用完全相同的参数执行过并返回结果。"
                f"请勿重复发起相同的工具调用，请直接根据已有检索信息作答，或调整检索参数（如关键词、过滤条件）后重试。"
            )
            logger.warning(f"Duplicate tool call blocked: {fp} (count={curr_consecutive_duplicate_calls})")
            tool_messages.append(
                ToolMessage(
                    content=json.dumps({"status": "duplicate_blocked", "tool_name": tool_name, "error": err_msg}, ensure_ascii=False),
                    tool_call_id=tc_id,
                    name=tool_name,
                )
            )
            curr_tool_call_count += 1
            traces.append({
                "event": "tool_duplicate_blocked",
                "tool_name": tool_name,
                "tool_call_id": tc_id,
                "fingerprint": fp,
                "consecutive_duplicate_calls": curr_consecutive_duplicate_calls,
                "error": err_msg,
                "timestamp": time.time(),
            })
            continue
        else:
            # Model made a new non-duplicate call, reset consecutive duplicates count
            curr_consecutive_duplicate_calls = 0

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

        # P0-1: Update consecutive parameter error counter
        # Counter ONLY increments on Pydantic ValidationError (error_type == 'param_validation').
        # Any successful Schema validation (even if _run raises execution exception or times out) resets it to 0.
        is_param_val_error = (
            getattr(tool_result, "error_type", None) == "param_validation"
            or tool_result.metadata.get("error_type") == "param_validation"
        )
        if is_param_val_error:
            curr_consecutive_param_errors += 1
        else:
            curr_consecutive_param_errors = 0
            # P0-2: Record successfully validated tool call fingerprint for anti-oscillation
            if fp not in executed_fps:
                executed_fps.append(fp)

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

    # 2. Process invalid_tool_calls (malformed JSON from LLM)
    if invalid_tool_calls:
        curr_consecutive_json_errors += 1
        for itc in invalid_tool_calls:
            tc_id = itc.get("id")
            if not tc_id:
                tc_id = f"invalid_{uuid.uuid4().hex[:8]}"
                itc["id"] = tc_id
            tool_name = itc.get("name", "")
            raw_args = itc.get("args")
            err = itc.get("error") or "Malformed JSON arguments"
            err_msg = f"Tool arguments JSON parse error: {err}. Please format tool arguments as valid JSON."
            logger.warning(f"Invalid tool call received for '{tool_name}' ({tc_id}): {err}")
            tool_messages.append(
                ToolMessage(
                    content=json.dumps({"status": "error", "tool_name": tool_name, "error": err_msg}, ensure_ascii=False),
                    tool_call_id=tc_id,
                    name=tool_name,
                )
            )
            curr_tool_call_count += 1
            traces.append({
                "event": "tool_malformed_json_error",
                "tool_name": tool_name,
                "tool_call_id": tc_id,
                "raw_args": str(raw_args)[:200] if raw_args is not None else "",
                "error": err_msg,
                "timestamp": time.time(),
            })

    # Sync Run-scoped evidence
    updated_evidence = [e.model_dump() for e in evidence_registry.get_all_evidence_items()]

    return {
        "messages": tool_messages,
        "tool_call_count": curr_tool_call_count,
        "consecutive_param_errors": curr_consecutive_param_errors,
        "consecutive_duplicate_calls": curr_consecutive_duplicate_calls,
        "consecutive_json_errors": curr_consecutive_json_errors,
        "executed_tool_fingerprints": executed_fps,
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
