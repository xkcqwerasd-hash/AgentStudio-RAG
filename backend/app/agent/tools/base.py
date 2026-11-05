import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type
from pydantic import BaseModel, ValidationError

from app.agent.tools.schemas import ToolResult
from app.core.logging import logger


class BaseTool(ABC):
    """
    Abstract base class for all Agent tools in AgentStudio-RAG.
    Enforces:
    1. Static metadata (name, description).
    2. Strongly-typed Pydantic argument schema validation.
    3. OpenAI-compatible Function Calling schema export.
    4. Async execution with automatic latency tracking and exception isolation.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Globally unique tool name."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Detailed description explaining when and how to call this tool."""
        pass

    @property
    @abstractmethod
    def args_schema(self) -> Type[BaseModel]:
        """Pydantic model defining the expected input arguments."""
        pass

    def to_openai_tool(self) -> Dict[str, Any]:
        """
        Export this tool's contract into standard OpenAI / DeepSeek Function Calling JSON schema.
        Cleans Pydantic metadata ($defs, title) to ensure maximum LLM provider compatibility.
        """
        raw_schema = self.args_schema.model_json_schema()
        properties: Dict[str, Any] = {}
        for prop_name, prop_val in raw_schema.get("properties", {}).items():
            cleaned_prop = {
                "type": prop_val.get("type", "string"),
                "description": prop_val.get("description", ""),
            }
            if "default" in prop_val:
                cleaned_prop["default"] = prop_val["default"]
            if "enum" in prop_val:
                cleaned_prop["enum"] = prop_val["enum"]
            properties[prop_name] = cleaned_prop

        parameters = {
            "type": "object",
            "properties": properties,
            "required": raw_schema.get("required", []),
        }

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }

    async def run(self, **kwargs) -> ToolResult:
        """
        Standard execution entrypoint:
        1. Validates inputs against args_schema.
        2. Times execution.
        3. Catches and converts any exception into a structured error result.
        """
        start_time = time.perf_counter()

        # 1. Pydantic parameter validation
        try:
            validated = self.args_schema(**kwargs)
        except ValidationError as val_err:
            duration_ms = (time.perf_counter() - start_time) * 1000
            err_msg = f"Invalid arguments for tool '{self.name}': {val_err.errors()}"
            logger.warning(err_msg)
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=err_msg,
                duration_ms=round(duration_ms, 2),
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000
            err_msg = f"Unexpected validation error for tool '{self.name}': {exc}"
            logger.error(err_msg)
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=err_msg,
                duration_ms=round(duration_ms, 2),
            )

        # 2. Execute concrete tool logic
        try:
            data = await self._run(**validated.model_dump())
            duration_ms = (time.perf_counter() - start_time) * 1000
            return ToolResult(
                tool_name=self.name,
                success=True,
                data=data,
                duration_ms=round(duration_ms, 2),
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000
            err_msg = f"Error executing tool '{self.name}': {exc}"
            logger.error(err_msg, exc_info=True)
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=str(exc),
                duration_ms=round(duration_ms, 2),
            )

    @abstractmethod
    async def _run(self, **kwargs) -> Any:
        """Concrete tool execution logic implemented by subclasses."""
        pass
