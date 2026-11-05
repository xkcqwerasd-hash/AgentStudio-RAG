from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import settings

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    db_ok = False
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "service": settings.PROJECT_NAME,
        "database_connected": db_ok,
        "llm_provider": settings.LLM_PROVIDER,
        "llm_configured": bool(settings.OPENAI_API_KEY) or settings.LLM_PROVIDER == "test_double",
        "embedding_provider": settings.EMBEDDING_PROVIDER,
        "embedding_model": settings.EMBEDDING_MODEL_NAME,
        "embedding_dimensions": settings.EMBEDDING_DIMENSIONS,
        "embedding_configured": bool(settings.EMBEDDING_API_KEY) or settings.EMBEDDING_PROVIDER in ("test_double", "local_bge"),
    }
