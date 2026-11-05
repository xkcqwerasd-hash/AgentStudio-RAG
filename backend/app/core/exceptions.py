from typing import Any, Dict, Optional
from fastapi import HTTPException, status


class AgentStudioException(HTTPException):
    def __init__(
        self,
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail: str = "Internal server error occurred",
        headers: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(status_code=status_code, detail=detail, headers=headers)


class KnowledgeBaseNotFoundError(AgentStudioException):
    def __init__(self, kb_id: str):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Knowledge base with ID '{kb_id}' not found.",
        )


class ConversationNotFoundError(AgentStudioException):
    def __init__(self, conv_id: str):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation with ID '{conv_id}' not found.",
        )


class DocumentNotFoundError(AgentStudioException):
    def __init__(self, doc_id: str):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document with ID '{doc_id}' not found.",
        )


class UnsupportedFileTypeError(AgentStudioException):
    def __init__(self, ext: str):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file format '{ext}'. Allowed formats: .pdf, .md, .txt",
        )


class FileTooLargeError(AgentStudioException):
    def __init__(self, max_size_mb: int):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File exceeds maximum allowed size of {max_size_mb} MB.",
        )


class DocumentAlreadyExistsError(AgentStudioException):
    def __init__(self, file_name: str, kb_name: str):
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Document with name '{file_name}' already exists in knowledge base '{kb_name}'. Please delete the existing document first or rename the file.",
        )


class AgentDefinitionNotFoundError(AgentStudioException):
    def __init__(self, agent_id: str):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent with ID '{agent_id}' not found.",
        )


class AgentRunNotFoundError(AgentStudioException):
    def __init__(self, run_id: str):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent run with ID '{run_id}' not found.",
        )


class InvalidToolConfigurationError(AgentStudioException):
    def __init__(self, tool_name: str):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tool '{tool_name}' is not registered or supported in ToolRegistry.",
        )


class VectorDimensionMismatchError(AgentStudioException):
    def __init__(self, query_dim: int, doc_dim: int):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Query vector dimension ({query_dim}) does not match chunk vector dimension ({doc_dim}).",
        )


class VectorModelMismatchError(AgentStudioException):
    def __init__(self, current_model: str, index_model: str, kb_id: str):
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Vector model mismatch: Current active embedding model is '{current_model}', "
                f"but knowledge base index was built with '{index_model}'. "
                f"Please trigger reindex for knowledge base '{kb_id}' to update vector embeddings."
            ),
        )


class LLMNotConfiguredError(AgentStudioException):
    def __init__(self, detail: str = "LLM API key is not configured. Please set OPENAI_API_KEY in backend/.env"):
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
        )


class EmbeddingServiceError(AgentStudioException):
    def __init__(self, detail: str = "Embedding service unavailable or failed"):
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
        )


class QueryTooLongError(AgentStudioException):
    def __init__(self, query_tokens: int, max_tokens: int):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"用户提问长度超限 (估算 {query_tokens} tokens, 最大允许 {max_tokens} tokens)，请精简问题后再试。",
        )


class ContextBudgetExceededError(AgentStudioException):
    def __init__(self, total_tokens: int, budget: int):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"上下文总长度超出模型预算 (实际 {total_tokens} tokens, 最大预算 {budget} tokens)。",
        )


class MemoryNotFoundError(AgentStudioException):
    def __init__(self, memory_id: str):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory item with ID '{memory_id}' not found.",
        )


class MemoryAccessForbiddenError(AgentStudioException):
    def __init__(self, detail: str = "Access to memory item is forbidden."):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=detail,
        )
