import asyncio
from pathlib import Path
import socket
from typing import AsyncGenerator
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.core.config import settings
from app.db.session import Base
import app.models  # Ensure all models are registered on Base.metadata
from app.main import app

# Ensure tests use deterministic offline providers by default
settings.LLM_PROVIDER = "test_double"
settings.EMBEDDING_PROVIDER = "test_double"
settings.MEMORY_SIMILARITY_THRESHOLD = 0.25

# Deterministic offline DNS mapping for external provider test domains
# Prevents host environment VPN/TUN Fake-IP (198.18.0.0/15) from interfering with Server mode unit tests.
_real_getaddrinfo = socket.getaddrinfo

def _deterministic_test_getaddrinfo(host, port, *args, **kwargs):
    if host in ("api.openai.com", "api.deepseek.com", "api.siliconflow.cn", "api.alice-ai.corp", "api.dev.corp"):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]
    return _real_getaddrinfo(host, port, *args, **kwargs)

socket.getaddrinfo = _deterministic_test_getaddrinfo

# Use in-memory SQLite for tests
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
)

test_session_factory = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# Patch global async_session_factory so background tasks use test in-memory database
from app.db import session as db_session_module
db_session_module.async_session_factory = test_session_factory


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
async def setup_test_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with test_session_factory() as session:
        yield session


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
