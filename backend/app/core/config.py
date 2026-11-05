import math
from collections import Counter
from pathlib import Path
from typing import Literal, Optional, Set
import os
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_project_root() -> Path:
    env_root = os.getenv("PROJECT_ROOT")
    if env_root:
        return Path(env_root).resolve()
    curr = Path(__file__).resolve().parent
    for p in [curr, *curr.parents]:
        if (p / "backend").is_dir() and (p / "frontend").is_dir():
            return p
    return Path(__file__).resolve().parent.parent.parent.parent


_PROJECT_ROOT = _find_project_root()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "backend/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Project metadata
    PROJECT_NAME: str = "AgentStudio-RAG"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = True

    # Deployment & Identity Mode (P1.2 / P1.3)
    DEPLOYMENT_MODE: Literal["local", "server"] = "local"
    DEFAULT_LOCAL_USER_ID: str = "usr_local_default"
    SERVER_AUTH_SECRET: str = "agentstudio_server_secret_key_change_in_production"
    SERVER_ALLOW_REGISTRATION: bool = True
    AUTO_FIRST_USER_ADMIN: bool = True
    PASSWORD_HASH_ITERATIONS: int = 600_000
    ALLOW_TEST_USER_OVERRIDE: bool = False
    # Explicit whitelist of internal host:port or hostnames allowed for system public model services in Server mode
    SERVER_TRUSTED_INTERNAL_HOSTS: str = ""

    @property
    def trusted_internal_hosts_set(self) -> Set[str]:
        if not self.SERVER_TRUSTED_INTERNAL_HOSTS:
            return set()
        return {h.strip().lower() for h in self.SERVER_TRUSTED_INTERNAL_HOSTS.split(",") if h.strip()}

    # Database & Storage
    # Default to SQLite for zero-configuration instant local running, or postgresql+asyncpg://...
    PROJECT_ROOT: Path = _PROJECT_ROOT
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/storage/agentstudio_rag.db"
    STORAGE_DIR: Path = _PROJECT_ROOT / "data" / "storage"
    UPLOAD_DIR: Path = _PROJECT_ROOT / "data" / "uploads"
    ATTACHMENTS_DIR: Path = _PROJECT_ROOT / "data" / "uploads" / "attachments"

    # Chat Attachments Defaults (P0.3)
    MAX_ATTACHMENT_SIZE_MB: int = 20
    MAX_ATTACHMENTS_PER_CONVERSATION: int = 10

    # LLM Settings (OpenAI-compatible)
    # Default to test_double if no key is provided
    LLM_PROVIDER: Literal["openai", "test_double"] = "openai"
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.deepseek.com"
    LLM_MODEL: str = "deepseek-chat"
    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_TOKENS: int = 2048
    MODEL_ENCRYPTION_KEY: Optional[str] = None

    # Embedding Settings
    # Supports "local_bge" (local ONNX BGE model), "openai" (OpenAI-compatible API), "test_double"
    EMBEDDING_PROVIDER: Literal["openai", "test_double", "local_bge"] = "local_bge"
    EMBEDDING_BASE_URL: str = "https://api.siliconflow.cn/v1"
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL_NAME: str = "BAAI/bge-small-zh-v1.5"
    EMBEDDING_DIMENSIONS: int = 512
    EMBEDDING_BATCH_SIZE: int = 32

    # Backwards compatibility properties
    @property
    def ROOT_DIR(self) -> Path:
        return self.PROJECT_ROOT

    @property
    def EMBEDDING_MODEL(self) -> str:
        return self.EMBEDDING_MODEL_NAME

    @property
    def EMBEDDING_DIMENSION(self) -> int:
        return self.EMBEDDING_DIMENSIONS

    # RAG Ingestion Defaults
    DEFAULT_CHUNK_SIZE: int = 500
    DEFAULT_CHUNK_OVERLAP: int = 50
    MAX_UPLOAD_SIZE_MB: int = 20

    # Retrieval Defaults
    DEFAULT_TOP_K: int = 4
    SIMILARITY_THRESHOLD: float = 0.1

    # RRF Hybrid Retrieval Defaults (M3.2)
    RRF_K: int = 60
    RRF_DENSE_WEIGHT: float = 1.0
    RRF_BM25_WEIGHT: float = 1.0
    RRF_CANDIDATE_FACTOR: int = 3
    RETRIEVAL_MODE: Literal["hybrid", "dense", "bm25"] = "hybrid"

    # Cross-Encoder Reranker Defaults (M3.3)
    RERANKER_ENABLED: bool = True
    RERANKER_PROVIDER: Literal["local_onnx", "test_double"] = "local_onnx"
    RERANKER_MODEL_NAME: str = "Xenova/bge-reranker-base"
    RERANKER_TOP_K: int = 4
    RERANKER_CANDIDATE_POOL_SIZE: int = 20
    RERANKER_BATCH_SIZE: int = 16
    RERANKER_TIMEOUT: float = 10.0
    RERANKER_DEVICE: str = "cpu"

    # Multi-turn Context Management Defaults
    MAX_HISTORY_TURNS: int = 5
    MAX_HISTORY_CHARS: int = 2000

    # Query Rewriting Defaults (M2.3)
    QUERY_REWRITING_ENABLED: bool = True
    QUERY_REWRITING_MAX_HISTORY_TURNS: int = 5
    QUERY_REWRITING_TIMEOUT: float = 10.0
    QUERY_REWRITING_MODEL: Optional[str] = None

    # Token Budget & Context Optimization Defaults (M2.4)
    MODEL_CONTEXT_WINDOW: int = 32768
    MAX_OUTPUT_TOKENS: int = 4096
    CONTEXT_SAFETY_MARGIN: int = 2048
    SYSTEM_PROMPT_BUDGET: int = 1024
    HISTORY_TOKEN_BUDGET: int = 4096
    RETRIEVAL_TOKEN_BUDGET: int = 8192
    MAX_QUERY_TOKENS: int = 2048

    # Long-term Memory Settings (P1.2)
    MEMORY_SIMILARITY_THRESHOLD: float = 0.35
    MEMORY_DEFAULT_TOP_K: int = 5
    MEMORY_TOKEN_BUDGET: int = 512


settings = Settings()

INSECURE_SECRET_SUBSTRINGS = (
    "agentstudio_server_secret_key_change_in_production",
    "change_in_production",
    "change_me",
    "placeholder",
    "example",
    "your_secret",
    "default_secret",
    "secret_key",
    "replace_with",
    "12345678",
)


def calculate_shannon_entropy(data: str) -> float:
    """Calculate the Shannon entropy of a string in bits per character."""
    if not data:
        return 0.0
    counts = Counter(data)
    total = len(data)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def validate_server_security_settings(cfg: Optional[Settings] = None) -> None:
    """
    Application-level security audit for Server deployment mode.
    Enforces that production server deployments cannot start with default,
    placeholder, or weak secrets.
    """
    conf = cfg or settings
    if conf.DEPLOYMENT_MODE == "server":
        secret = (conf.SERVER_AUTH_SECRET or "").strip()
        if not secret:
            raise RuntimeError(
                "Security Check Failed: SERVER_AUTH_SECRET cannot be empty in 'server' deployment mode. "
                "Please configure a cryptographically secure secret (minimum 32 characters, "
                "recommendation: generate via secrets.token_urlsafe(32))."
            )

        if secret == "agentstudio_server_secret_key_change_in_production":
            raise RuntimeError(
                "Security Check Failed: In Server deployment mode, SERVER_AUTH_SECRET must be explicitly configured "
                "and cannot use the default development key. "
                "Generate one via: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )

        if len(secret) < 32:
            raise RuntimeError(
                f"Security Check Failed: SERVER_AUTH_SECRET is too short ({len(secret)} chars). "
                "Server deployment mode requires a cryptographically strong secret of at least 32 characters "
                "(recommendation: generate via secrets.token_urlsafe(32))."
            )

        lower_secret = secret.lower()
        for placeholder in INSECURE_SECRET_SUBSTRINGS:
            if placeholder in lower_secret:
                raise RuntimeError(
                    f"Security Check Failed: SERVER_AUTH_SECRET contains insecure placeholder or default '{placeholder}'. "
                    "Server deployment mode requires a dedicated, randomly generated secret."
                )

        # Check for repetitive substring patterns (e.g. "abab...", "12341234...", "passpass...")
        for chunk_len in range(1, 9):
            if len(secret) >= chunk_len * 3:
                prefix = secret[:chunk_len]
                repeated = prefix * (len(secret) // chunk_len)
                if secret.startswith(repeated) and len(repeated) >= len(secret) - chunk_len:
                    raise RuntimeError(
                        f"Security Check Failed: SERVER_AUTH_SECRET contains repetitive sequence '{prefix}'. "
                        "Please use a cryptographically random secret."
                    )

        # Check for predictable alphanumeric / keyboard runs
        predictable_runs = [
            "abcdefghijklmnopqrstuvwxyz",
            "01234567890123456789",
            "qwertyuiopasdfghjklzxcvbnm",
        ]
        for run in predictable_runs:
            if run[:16] in lower_secret:
                raise RuntimeError(
                    f"Security Check Failed: SERVER_AUTH_SECRET contains predictable pattern '{run[:16]}...'. "
                    "Please use a truly random string."
                )

        # Calculate true Shannon entropy (bits per character)
        entropy = calculate_shannon_entropy(secret)
        if entropy < 3.2:
            raise RuntimeError(
                f"Security Check Failed: SERVER_AUTH_SECRET has low Shannon entropy ({entropy:.2f} bits/char < 3.20). "
                "Please generate a truly random secret, e.g. via secrets.token_urlsafe(32)."
            )

        # Check MODEL_ENCRYPTION_KEY if provided
        if conf.MODEL_ENCRYPTION_KEY:
            enc_key = conf.MODEL_ENCRYPTION_KEY.strip().lower()
            for placeholder in ("change_me", "placeholder", "example", "default", "12345678"):
                if placeholder in enc_key:
                    raise RuntimeError(
                        f"Security Check Failed: MODEL_ENCRYPTION_KEY contains insecure placeholder '{placeholder}'."
                    )


# Ensure directories exist
settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
settings.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
