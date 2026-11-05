from typing import Annotated, Any, Dict, List, Optional, Sequence
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """
    Standard state schema for LangGraph Agent Runtime in AgentStudio-RAG.
    Strictly manages message sequence, run identifiers, evidence accumulation,
    execution counters, lifecycle statuses, citations, and observable traces.
    """
    # 1. Message sequence with LangGraph add_messages reducer
    messages: Annotated[Sequence[BaseMessage], add_messages]

    # 2. Run identifier & boundaries
    run_id: str
    user_id: Optional[str]
    conversation_id: Optional[str]
    knowledge_base_id: Optional[str]
    attachment_ids: Optional[List[str]]
    enabled_tools: List[str]

    # 3. Accumulated structured evidence items for this run
    evidence: List[Dict[str, Any]]

    # 4. Step & tool execution counters
    execution_steps: int
    tool_call_count: int

    # 5. Runtime lifecycle status & diagnostics
    # "running" | "completed" | "failed" | "timeout" | "cancelled" | "max_steps_reached"
    status: str
    error: Optional[str]
    final_answer: Optional[str]
    citations: List[Dict[str, Any]]

    # 6. Observable trace events timeline
    trace_events: List[Dict[str, Any]]

    # 7. Long-term memory context & observability (P1.2)
    memory_context: Optional[str]
    memory_info: Optional[Dict[str, Any]]
