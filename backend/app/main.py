import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.api_router import api_v1_router
from app.core.config import settings, validate_server_security_settings
from app.core.exceptions import AgentStudioException
from app.core.logging import logger, setup_logging
from app.db.session import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    # Application-level startup security audit
    validate_server_security_settings(settings)
    if not os.getenv("SKIP_LIFESPAN_DB_INIT"):
        logger.info("Initializing AgentStudio-RAG Database schema...")
        await init_db()
        logger.info("Database initialized successfully.")
    yield
    logger.info("AgentStudio-RAG service shutting down...")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0",
    description="Enterprise Agentic RAG System in Python - Backend API",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AgentStudioException)
async def agentstudio_exception_handler(request: Request, exc: AgentStudioException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled Exception on {request.method} {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error. Please check server logs."},
    )


app.include_router(api_v1_router, prefix=settings.API_V1_PREFIX)


@app.get("/")
async def root():
    return {
        "message": "Welcome to AgentStudio-RAG API",
        "docs_url": "/docs",
        "health_check": f"{settings.API_V1_PREFIX}/health",
    }
