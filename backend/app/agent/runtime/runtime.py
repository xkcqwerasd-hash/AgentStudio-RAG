import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.runtime.graph import create_agent_graph
from app.agent.runtime.state import AgentState
from app.agent.tools.evidence import EvidenceRegistry
from app.agent.tools.registry import ToolRegistry
from app.core.logging import logger
from app.llm.base import BaseLLMClient
from app.llm.openai_client import get_llm_client


class LangGraphAgentRuntime:
    """
    High-level execution runtime for LangGraph Agent in AgentStudio-RAG.
    Coordinates:
    1. Run-scoped EvidenceRegistry creation & isolation per execution.
    2. Trusted runtime boundaries (knowledge_base_id & tool permissions).
    3. Graph lifecycle execution with configurable max_steps & max_tool_calls.
    4. Timeout monitoring (tool_timeout and run_timeout).
    5. Cancellation safety (asyncio.CancelledError transparent re-raising).
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        llm_client: Optional[BaseLLMClient] = None,
        db_session: Optional[AsyncSession] = None,
        max_steps: int = 5,
        max_tool_calls: int = 10,
        tool_timeout: float = 30.0,
        run_timeout: float = 60.0,
    ):
        self.tool_registry = tool_registry
        self.llm_client = llm_client or get_llm_client()
        self.db_session = db_session
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.tool_timeout = tool_timeout
        self.run_timeout = run_timeout
        self.graph = create_agent_graph()
        self.last_final_state: Optional[AgentState] = None

    async def ainvoke(
        self,
        query: str,
        knowledge_base_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        attachment_ids: Optional[List[str]] = None,
        enabled_tools: Optional[List[str]] = None,
        run_id: Optional[str] = None,
        initial_messages: Optional[List[BaseMessage]] = None,
        evidence_registry: Optional[EvidenceRegistry] = None,
        memory_context: Optional[str] = None,
        memory_info: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
    ) -> AgentState:
        """
        Execute an agent task end-to-end within an isolated Run boundary.
        """
        active_run_id = run_id or str(uuid.uuid4())
        # Fresh Run-scoped EvidenceRegistry instance strictly isolated from other runs
        run_evidence_registry = evidence_registry if evidence_registry is not None else EvidenceRegistry()

        # Determine tools
        active_tools = enabled_tools if enabled_tools is not None else self.tool_registry.list_tool_names()

        # Build initial messages
        if initial_messages:
            messages = list(initial_messages)
        else:
            messages = []
            if memory_context:
                messages.append(SystemMessage(content=memory_context))
            messages.append(HumanMessage(content=query))

        initial_state: AgentState = {
            "messages": messages,
            "run_id": active_run_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "knowledge_base_id": knowledge_base_id,
            "attachment_ids": attachment_ids or [],
            "enabled_tools": active_tools,
            "evidence": [],
            "execution_steps": 0,
            "tool_call_count": 0,
            "consecutive_param_errors": 0,
            "consecutive_duplicate_calls": 0,
            "consecutive_json_errors": 0,
            "executed_tool_fingerprints": [],
            "status": "running",
            "error": None,
            "final_answer": None,
            "citations": [],
            "memory_context": memory_context,
            "memory_info": memory_info,
            "trace_events": [
                {
                    "event": "run_started",
                    "run_id": active_run_id,
                    "query": query,
                    "conversation_id": conversation_id,
                    "knowledge_base_id": knowledge_base_id,
                    "enabled_tools": active_tools,
                    "timestamp": time.time(),
                }
            ],
        }

        config = {
            "configurable": {
                "evidence_registry": run_evidence_registry,
                "tool_registry": self.tool_registry,
                "llm_client": self.llm_client,
                "db_session": self.db_session,
                "max_steps": self.max_steps,
                "max_tool_calls": self.max_tool_calls,
                "tool_timeout": self.tool_timeout,
            }
        }

        start_time = time.perf_counter()
        logger.info(f"Starting Agent run '{active_run_id}' (KB: {knowledge_base_id}, tools: {active_tools})")

        try:
            final_state = await asyncio.wait_for(
                self.graph.ainvoke(initial_state, config),
                timeout=self.run_timeout,
            )
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.info(f"Agent run '{active_run_id}' completed with status '{final_state.get('status')}' in {elapsed_ms}ms")
            self.last_final_state = final_state
            return final_state

        except asyncio.CancelledError:
            logger.warning(f"Agent run '{active_run_id}' was cancelled by caller.")
            # Explicit cancellation propagation: do not swallow!
            raise

        except asyncio.TimeoutError:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            err_msg = f"Agent run '{active_run_id}' exceeded timeout limit of {self.run_timeout}s."
            logger.error(err_msg)
            initial_state["status"] = "timeout"
            initial_state["error"] = err_msg
            initial_state["trace_events"].append({
                "event": "run_timeout",
                "timeout": self.run_timeout,
                "elapsed_ms": elapsed_ms,
                "timestamp": time.time(),
            })
            self.last_final_state = initial_state
            return initial_state

        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.error(f"Agent run '{active_run_id}' failed fatally: {exc}", exc_info=True)
            initial_state["status"] = "failed"
            initial_state["error"] = str(exc)
            initial_state["trace_events"].append({
                "event": "run_failed",
                "error": str(exc),
                "elapsed_ms": elapsed_ms,
                "timestamp": time.time(),
            })
            self.last_final_state = initial_state
            return initial_state

    async def astream_events(
        self,
        query: str,
        knowledge_base_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        attachment_ids: Optional[List[str]] = None,
        enabled_tools: Optional[List[str]] = None,
        run_id: Optional[str] = None,
        initial_messages: Optional[List[BaseMessage]] = None,
        evidence_registry: Optional[EvidenceRegistry] = None,
        memory_context: Optional[str] = None,
        memory_info: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
    ):
        """
        Execute an agent task and yield real-time structured execution events for SSE.
        Directly tracks state graph transitions without fake chunking.
        """
        active_run_id = run_id or str(uuid.uuid4())
        run_evidence_registry = evidence_registry if evidence_registry is not None else EvidenceRegistry()
        active_tools = enabled_tools if enabled_tools is not None else self.tool_registry.list_tool_names()

        if initial_messages:
            messages = list(initial_messages)
        else:
            messages = []
            if memory_context:
                messages.append(SystemMessage(content=memory_context))
            messages.append(HumanMessage(content=query))

        initial_state: AgentState = {
            "messages": messages,
            "run_id": active_run_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "knowledge_base_id": knowledge_base_id,
            "attachment_ids": attachment_ids or [],
            "enabled_tools": active_tools,
            "evidence": [],
            "execution_steps": 0,
            "tool_call_count": 0,
            "consecutive_param_errors": 0,
            "consecutive_duplicate_calls": 0,
            "consecutive_json_errors": 0,
            "executed_tool_fingerprints": [],
            "status": "running",
            "error": None,
            "final_answer": None,
            "citations": [],
            "memory_context": memory_context,
            "memory_info": memory_info,
            "trace_events": [
                {
                    "event": "run_started",
                    "run_id": active_run_id,
                    "query": query,
                    "conversation_id": conversation_id,
                    "knowledge_base_id": knowledge_base_id,
                    "enabled_tools": active_tools,
                    "timestamp": time.time(),
                }
            ],
        }

        config = {
            "configurable": {
                "evidence_registry": run_evidence_registry,
                "tool_registry": self.tool_registry,
                "llm_client": self.llm_client,
                "db_session": self.db_session,
                "max_steps": self.max_steps,
                "max_tool_calls": self.max_tool_calls,
                "tool_timeout": self.tool_timeout,
            }
        }

        start_time = time.perf_counter()
        yield {
            "event": "run_started",
            "data": {
                "run_id": active_run_id,
                "query": query,
                "knowledge_base_id": knowledge_base_id,
                "enabled_tools": active_tools,
                "timestamp": time.time(),
            },
        }

        accumulated_state: AgentState = dict(initial_state)  # copy
        processed_tool_calls = set()

        try:
            async for update in self.graph.astream(initial_state, config, stream_mode="updates"):
                for node_name, node_output in update.items():
                    # Merge updates into accumulated_state
                    for k, v in node_output.items():
                        if k == "messages":
                            accumulated_state["messages"] = list(accumulated_state.get("messages", [])) + list(v)
                        else:
                            accumulated_state[k] = v

                    if node_name == "agent_node":
                        ai_msgs = node_output.get("messages", [])
                        ai_msg = ai_msgs[0] if ai_msgs else None
                        step = node_output.get("execution_steps", accumulated_state.get("execution_steps", 1))
                        tcs = (getattr(ai_msg, "tool_calls", []) or []) if ai_msg else []

                        yield {
                            "event": "agent_step_completed",
                            "data": {
                                "run_id": active_run_id,
                                "step": step,
                                "tool_calls_count": len(tcs),
                                "decision": "call_tools" if tcs else "final_response",
                                "tools": [tc.get("name") for tc in tcs],
                                "content_preview": (getattr(ai_msg, "content", "") or "")[:120],
                                "timestamp": time.time(),
                            },
                        }

                        for tc in tcs:
                            yield {
                                "event": "tool_call_started",
                                "data": {
                                    "run_id": active_run_id,
                                    "tool_call_id": tc.get("id"),
                                    "tool_name": tc.get("name"),
                                    "arguments": tc.get("args"),
                                    "timestamp": time.time(),
                                },
                            }

                    elif node_name == "tool_node":
                        traces = node_output.get("trace_events", [])
                        for tr in traces:
                            if tr.get("event") == "tool_executed":
                                tc_id = tr.get("tool_call_id")
                                if tc_id not in processed_tool_calls:
                                    processed_tool_calls.add(tc_id)
                                    yield {
                                        "event": "tool_call_completed",
                                        "data": {
                                            "run_id": active_run_id,
                                            "tool_call_id": tc_id,
                                            "tool_name": tr.get("tool_name"),
                                            "success": tr.get("success"),
                                            "duration_ms": tr.get("duration_ms"),
                                            "error": tr.get("error"),
                                            "source_type": tr.get("source_type"),
                                            "attachment_id": tr.get("attachment_id"),
                                            "original_filename": tr.get("original_filename"),
                                            "retrieval_mode": tr.get("retrieval_mode"),
                                            "retrieved_chunks": tr.get("retrieved_chunks"),
                                            "returned_chunks": tr.get("returned_chunks"),
                                            "retrieval_duration_ms": tr.get("retrieval_duration_ms"),
                                            "evidence_refs": tr.get("evidence_refs"),
                                            "evidence_tokens": tr.get("evidence_tokens"),
                                            "timestamp": time.time(),
                                        },
                                    }

                    elif node_name == "force_stop_node":
                        yield {
                            "event": "agent_step_completed",
                            "data": {
                                "run_id": active_run_id,
                                "step": "force_stop",
                                "decision": "forced_summary",
                                "reason": "max_steps_reached",
                                "timestamp": time.time(),
                            },
                        }

                    elif node_name == "citation_verifier_node":
                        cits = node_output.get("citations", [])
                        ans = node_output.get("final_answer", "")
                        status = node_output.get("status", "completed")
                        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

                        yield {
                            "event": "citation_verified",
                            "data": {
                                "run_id": active_run_id,
                                "citations_count": len(cits),
                                "citations": cits,
                                "timestamp": time.time(),
                            },
                        }

                        yield {
                            "event": "final_answer",
                            "data": {
                                "run_id": active_run_id,
                                "final_answer": ans,
                                "timestamp": time.time(),
                            },
                        }

                        yield {
                            "event": "trace",
                            "data": {
                                "run_id": active_run_id,
                                "execution_steps": accumulated_state.get("execution_steps", 0),
                                "tool_call_count": accumulated_state.get("tool_call_count", 0),
                                "duration_ms": elapsed_ms,
                                "trace_events": accumulated_state.get("trace_events", []),
                                "timestamp": time.time(),
                            },
                        }

                        yield {
                            "event": "run_completed",
                            "data": {
                                "run_id": active_run_id,
                                "status": status,
                                "final_answer": ans,
                                "duration_ms": elapsed_ms,
                                "timestamp": time.time(),
                            },
                        }

            self.last_final_state = accumulated_state

        except asyncio.CancelledError:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            accumulated_state["status"] = "cancelled"
            self.last_final_state = accumulated_state
            yield {
                "event": "run_cancelled",
                "data": {
                    "run_id": active_run_id,
                    "duration_ms": elapsed_ms,
                    "timestamp": time.time(),
                },
            }
            raise

        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            accumulated_state["status"] = "failed"
            accumulated_state["error"] = str(exc)
            self.last_final_state = accumulated_state
            yield {
                "event": "run_failed",
                "data": {
                    "run_id": active_run_id,
                    "error": str(exc),
                    "duration_ms": elapsed_ms,
                    "timestamp": time.time(),
                },
            }
