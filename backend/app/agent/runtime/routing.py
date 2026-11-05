from typing import Literal, Optional
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from app.agent.runtime.state import AgentState
from app.core.logging import logger


def route_agent_decision(
    state: AgentState,
    config: Optional[RunnableConfig] = None,
) -> Literal["tool_node", "force_stop_node", "citation_verifier_node"]:
    """
    Conditional routing function following Agent Node execution:
    1. If execution failed fatally in LLM call, bypass to citation_verifier_node.
    2. If the model emitted tool_calls:
       - If steps or tool call limits are reached: route to force_stop_node.
       - Otherwise: route to tool_node.
    3. If no tool_calls emitted (model generated direct text): route to citation_verifier_node.
    """
    if state.get("status") == "failed":
        return "citation_verifier_node"

    messages = state.get("messages", [])
    if not messages:
        return "citation_verifier_node"

    last_message = messages[-1]
    if not isinstance(last_message, AIMessage):
        return "citation_verifier_node"

    tool_calls = getattr(last_message, "tool_calls", []) or []
    if tool_calls:
        configurable = (config or {}).get("configurable", {})
        max_steps = configurable.get("max_steps", 5)
        max_tool_calls = configurable.get("max_tool_calls", 10)

        curr_steps = state.get("execution_steps", 0)
        curr_tool_calls = state.get("tool_call_count", 0)

        if curr_steps >= max_steps or curr_tool_calls >= max_tool_calls:
            logger.info(
                f"Agent run reached limit (steps: {curr_steps}/{max_steps}, "
                f"tool_calls: {curr_tool_calls}/{max_tool_calls}). Routing to force_stop_node."
            )
            return "force_stop_node"

        return "tool_node"

    return "citation_verifier_node"
