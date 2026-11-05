from fastapi import APIRouter

from app.api.v1.agent import router as agent_router
from app.api.v1.attachment import router as attachment_router
from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.document import router as document_router
from app.api.v1.health import router as health_router
from app.api.v1.knowledge import router as knowledge_router
from app.api.v1.memory import router as memory_router
from app.api.v1.models import router as models_router
from app.api.v1.retrieval import router as retrieval_router

api_v1_router = APIRouter()

api_v1_router.include_router(health_router)
api_v1_router.include_router(knowledge_router)
api_v1_router.include_router(document_router)
api_v1_router.include_router(retrieval_router)
api_v1_router.include_router(chat_router)
api_v1_router.include_router(agent_router)
api_v1_router.include_router(models_router)
api_v1_router.include_router(attachment_router)
api_v1_router.include_router(memory_router)
api_v1_router.include_router(auth_router)
