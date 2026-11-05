from langgraph.graph import END, START, StateGraph

from app.agent.runtime.nodes import (
    agent_node,
    citation_verifier_node,
    force_stop_node,
    tool_node,
)
from app.agent.runtime.routing import route_agent_decision
from app.agent.runtime.state import AgentState


def create_agent_graph():
    """
    Construct and compile the enterprise LangGraph Agent StateGraph.
    Topology:
      START
        │
        ▼
    agent_node ◄──────┐
        │             │
        ▼ (conditional)
      route ──────────┴─► tool_node
        │
        ├─► force_stop_node ──┐
        │                     │
        ▼                     ▼
    citation_verifier_node ◄──┘
        │
        ▼
       END
    """
    builder = StateGraph(AgentState)

    builder.add_node("agent_node", agent_node)
    builder.add_node("tool_node", tool_node)
    builder.add_node("force_stop_node", force_stop_node)
    builder.add_node("citation_verifier_node", citation_verifier_node)

    builder.add_edge(START, "agent_node")

    builder.add_conditional_edges(
        "agent_node",
        route_agent_decision,
        {
            "tool_node": "tool_node",
            "force_stop_node": "force_stop_node",
            "citation_verifier_node": "citation_verifier_node",
        },
    )

    builder.add_edge("tool_node", "agent_node")
    builder.add_edge("force_stop_node", "citation_verifier_node")
    builder.add_edge("citation_verifier_node", END)

    return builder.compile()
