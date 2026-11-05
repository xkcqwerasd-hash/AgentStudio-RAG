from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, List, Optional, Union
from langchain_core.messages import AIMessage, BaseMessage


class BaseEmbeddingClient(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the embedding provider (e.g. 'local_bge', 'openai', 'test_double')."""
        pass

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Identifier of the embedding model."""
        pass

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Vector dimensions produced by this embedding client."""
        pass

    @abstractmethod
    async def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Generate dense vector embeddings for a list of document chunks."""
        pass

    @abstractmethod
    async def embed_query(self, text: str) -> List[float]:
        """Generate dense vector embedding for a query string."""
        pass


class BaseLLMClient(ABC):
    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate text completion from LLM."""
        pass

    @abstractmethod
    async def generate_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """Yield incremental token chunks from LLM."""
        pass

    @abstractmethod
    async def generate_with_tools(
        self,
        messages: List[Union[Dict[str, Any], BaseMessage]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Union[str, Dict[str, Any]] = "auto",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AIMessage:
        """Generate response with native Function Calling / Tool Calling support."""
        pass
