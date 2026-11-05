import json
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """
    Standardized result emitted by any tool execution.
    Unified across all tool types, supporting structured data, error states, and latency tracking.
    """
    tool_name: str
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def to_tool_message_content(self) -> str:
        """
        Format result into an LLM-interpretable string for ToolMessage / Observation.
        """
        if not self.success:
            return json.dumps({
                "status": "error",
                "tool_name": self.tool_name,
                "error": self.error or "Unknown error occurred during tool execution.",
            }, ensure_ascii=False)

        if isinstance(self.data, (dict, list)):
            return json.dumps(self.data, ensure_ascii=False)
        return str(self.data) if self.data is not None else "{}"


class FunctionParameters(BaseModel):
    type: str = "object"
    properties: Dict[str, Any] = Field(default_factory=dict)
    required: List[str] = Field(default_factory=list)


class FunctionDefinition(BaseModel):
    name: str
    description: str
    parameters: Dict[str, Any]


class OpenAIToolDefinition(BaseModel):
    """
    OpenAI-compatible Function Calling tool definition schema.
    Directly compatible with DeepSeek-V3, GPT-4o, and LangChain bind_tools.
    """
    type: Literal["function"] = "function"
    function: FunctionDefinition
