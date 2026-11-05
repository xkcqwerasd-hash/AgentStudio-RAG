import json
from typing import Any, Dict, List, Optional, Union

from app.agent.tools.base import BaseTool
from app.agent.tools.schemas import ToolResult
from app.core.logging import logger


class DuplicateToolError(ValueError):
    """Raised when registering a tool whose name is already present in the registry."""
    pass


class ToolNotFoundError(KeyError):
    """Raised when looking up an unregistered tool name."""
    pass


class ToolRegistry:
    """
    Central tool registry and discovery system for AgentStudio-RAG.
    Capabilities:
    1. Tool registration with strict name uniqueness verification.
    2. Dynamic export to OpenAI Function Calling JSON schemas.
    3. Safe dispatching and execution with argument parsing and unregistered tool rejection.
    4. Isolation from eval / exec or dynamic string code execution.
    """

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool. Rejects duplicate tool names to guarantee deterministic dispatch."""
        if tool.name in self._tools:
            raise DuplicateToolError(
                f"Tool with name '{tool.name}' is already registered in ToolRegistry. "
                f"Existing: {self._tools[tool.name].__class__.__name__}, Attempted: {tool.__class__.__name__}"
            )
        self._tools[tool.name] = tool
        logger.info(f"Registered tool '{tool.name}' in ToolRegistry.")

    def unregister(self, name: str) -> None:
        """Unregister a tool by name if present."""
        if name in self._tools:
            del self._tools[name]
            logger.info(f"Unregistered tool '{name}' from ToolRegistry.")

    def get(self, name: str) -> Optional[BaseTool]:
        """Lookup tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check whether a tool is registered."""
        return name in self._tools

    def list_tools(self) -> List[BaseTool]:
        """Return all registered tool instances."""
        return list(self._tools.values())

    def list_tool_names(self) -> List[str]:
        """Return all registered tool names."""
        return list(self._tools.keys())

    def to_openai_tools(self) -> List[Dict[str, Any]]:
        """Export all registered tools into OpenAI-compatible tools schema list."""
        return [tool.to_openai_tool() for tool in self._tools.values()]

    async def execute(
        self,
        name: str,
        arguments: Union[str, Dict[str, Any], None] = None,
    ) -> ToolResult:
        """
        Execute a registered tool by name with safety checks:
        1. Safe rejection if tool is not registered.
        2. Safe JSON string deserialization if arguments are provided as JSON string by LLM.
        3. Pydantic validation and timed execution via tool.run().
        """
        tool = self.get(name)
        if not tool:
            err_msg = f"Security rejection: Tool '{name}' is not registered in ToolRegistry."
            logger.warning(err_msg)
            return ToolResult(
                tool_name=name,
                success=False,
                error=err_msg,
            )

        # Parse string arguments if emitted by LLM tool_calls[i].function.arguments
        parsed_args: Dict[str, Any] = {}
        if isinstance(arguments, str):
            clean_str = arguments.strip()
            if clean_str:
                try:
                    parsed_args = json.loads(clean_str)
                    if not isinstance(parsed_args, dict):
                        return ToolResult(
                            tool_name=name,
                            success=False,
                            error=f"Tool arguments must be a JSON object, got {type(parsed_args).__name__}.",
                        )
                except json.JSONDecodeError as json_err:
                    err_msg = f"Malformed JSON in tool arguments for '{name}': {json_err}"
                    logger.warning(err_msg)
                    return ToolResult(
                        tool_name=name,
                        success=False,
                        error=err_msg,
                    )
        elif isinstance(arguments, dict):
            parsed_args = arguments
        elif arguments is not None:
            return ToolResult(
                tool_name=name,
                success=False,
                error=f"Unsupported arguments type: {type(arguments).__name__}",
            )

        return await tool.run(**parsed_args)


def get_default_tool_registry() -> ToolRegistry:
    """
    Factory creating a default ToolRegistry pre-registered with standard enterprise tools.
    """
    from app.agent.tools.attachment_search import SearchConversationAttachmentsTool
    from app.agent.tools.knowledge_search import SearchKnowledgeBaseTool

    registry = ToolRegistry()
    registry.register(SearchKnowledgeBaseTool())
    registry.register(SearchConversationAttachmentsTool())
    return registry
