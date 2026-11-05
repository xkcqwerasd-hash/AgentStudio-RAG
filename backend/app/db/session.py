from collections.abc import AsyncGenerator
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


# SQLite connect args
connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args=connect_args,
    future=True,
)


# Enforce foreign key constraints for SQLite connections
@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if settings.DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    # Ensure all SQLAlchemy models are imported and registered with Base.metadata
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Automatic schema migration for existing SQLite & PostgreSQL databases
        def _migrate_schema_columns(sync_conn):
            dialect_name = sync_conn.dialect.name.lower()

            def get_columns(table_name: str) -> set:
                try:
                    if "sqlite" in dialect_name:
                        res = sync_conn.execute(text(f"PRAGMA table_info({table_name});"))
                        return {row[1] for row in res.fetchall()}
                    else:
                        # PostgreSQL
                        res = sync_conn.execute(
                            text(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table_name}';")
                        )
                        return {row[0] for row in res.fetchall()}
                except Exception:
                    return set()

            # 1. Check knowledge_bases columns
            kb_cols = get_columns("knowledge_bases")
            if kb_cols:
                if "embedding_provider" not in kb_cols:
                    sync_conn.execute(text("ALTER TABLE knowledge_bases ADD COLUMN embedding_provider VARCHAR(32) DEFAULT 'local_bge';"))
                if "embedding_dimensions" not in kb_cols:
                    sync_conn.execute(text("ALTER TABLE knowledge_bases ADD COLUMN embedding_dimensions INTEGER DEFAULT 512;"))
                if "index_version" not in kb_cols:
                    sync_conn.execute(text("ALTER TABLE knowledge_bases ADD COLUMN index_version INTEGER DEFAULT 1;"))
                if "owner_id" not in kb_cols:
                    sync_conn.execute(text(f"ALTER TABLE knowledge_bases ADD COLUMN owner_id VARCHAR(64) DEFAULT '{settings.DEFAULT_LOCAL_USER_ID}';"))
                    try:
                        sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_knowledge_bases_owner_id ON knowledge_bases (owner_id);"))
                    except Exception:
                        pass
                    sync_conn.execute(text(f"UPDATE knowledge_bases SET owner_id = '{settings.DEFAULT_LOCAL_USER_ID}' WHERE owner_id IS NULL;"))

            # 2. Check conversations columns
            conv_cols = get_columns("conversations")
            if conv_cols:
                if "owner_id" not in conv_cols:
                    sync_conn.execute(text(f"ALTER TABLE conversations ADD COLUMN owner_id VARCHAR(64) DEFAULT '{settings.DEFAULT_LOCAL_USER_ID}';"))
                    try:
                        sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_conversations_owner_id ON conversations (owner_id);"))
                    except Exception:
                        pass
                    sync_conn.execute(text(f"UPDATE conversations SET owner_id = '{settings.DEFAULT_LOCAL_USER_ID}' WHERE owner_id IS NULL;"))

            # 3. Check model_providers columns
            mp_cols = get_columns("model_providers")
            if mp_cols:
                if "owner_id" not in mp_cols:
                    sync_conn.execute(text("ALTER TABLE model_providers ADD COLUMN owner_id VARCHAR(64) DEFAULT NULL;"))
                    try:
                        sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_model_providers_owner_id ON model_providers (owner_id);"))
                    except Exception:
                        pass

            # 4. Check agent_definitions columns
            ad_cols = get_columns("agent_definitions")
            if ad_cols:
                if "owner_id" not in ad_cols:
                    sync_conn.execute(text("ALTER TABLE agent_definitions ADD COLUMN owner_id VARCHAR(64) DEFAULT NULL;"))
                    try:
                        sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_definitions_owner_id ON agent_definitions (owner_id);"))
                    except Exception:
                        pass

            # 5. Check agent_runs columns
            ar_cols = get_columns("agent_runs")
            if ar_cols:
                if "owner_id" not in ar_cols:
                    sync_conn.execute(text(f"ALTER TABLE agent_runs ADD COLUMN owner_id VARCHAR(64) DEFAULT '{settings.DEFAULT_LOCAL_USER_ID}';"))
                    try:
                        sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_runs_owner_id ON agent_runs (owner_id);"))
                    except Exception:
                        pass
                    sync_conn.execute(text(f"UPDATE agent_runs SET owner_id = '{settings.DEFAULT_LOCAL_USER_ID}' WHERE owner_id IS NULL;"))

        await conn.run_sync(_migrate_schema_columns)

    # Ensure default local user exists ONLY for local mode compatibility
    # In server mode, usr_local_default must NOT be unconditionally created or exposed.
    if settings.DEPLOYMENT_MODE == "local":
        async with async_session_factory() as session:
            from app.core.auth import get_or_create_local_user
            import logging
            try:
                await get_or_create_local_user(session)
            except Exception as e:
                logging.getLogger(__name__).warning("Initial local user setup note: %s", e)
