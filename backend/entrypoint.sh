#!/bin/sh
set -e

echo "=== AgentStudio-RAG Backend Bootstrapping ==="
echo "Deployment Mode: ${DEPLOYMENT_MODE:-local}"

# 1. Server Mode Strict Configuration Validation
if [ "$DEPLOYMENT_MODE" = "server" ]; then
    if [ -z "$SERVER_AUTH_SECRET" ] || [ "$SERVER_AUTH_SECRET" = "agentstudio_server_secret_key_change_in_production" ]; then
        echo "[FATAL ERROR] In Server mode, SERVER_AUTH_SECRET must be explicitly set to a secure, unique secret."
        echo "Refusing to start with missing or default secret."
        exit 1
    fi

    if [ -z "$MODEL_ENCRYPTION_KEY" ]; then
        echo "[WARNING] MODEL_ENCRYPTION_KEY is not set. A master key file will be generated in persistent storage."
    fi
fi

# 2. Ensure runtime data & cache directories exist
mkdir -p /app/data/storage /app/data/uploads /app/data/uploads/attachments
mkdir -p /app/models_cache/huggingface /app/models_cache/fastembed

# 3. Database Initialization & Schema Migration (Guaranteed Single Execution)
echo "[INFO] Running database schema migration and initialization..."
python -c "
import asyncio
import sys
from app.db.session import init_db

try:
    asyncio.run(init_db())
    print('[SUCCESS] Database initialized and verified.')
except Exception as e:
    print(f'[FATAL ERROR] Failed to initialize database: {e}', file=sys.stderr)
    sys.exit(1)
"

# Prevent uvicorn workers from redundantly running concurrent DDL migrations
export SKIP_LIFESPAN_DB_INIT=true

# 4. Determine Worker Count
if [ "$DEPLOYMENT_MODE" = "local" ]; then
    WORKERS=1
    echo "[INFO] Local mode: bound to 1 worker to ensure SQLite single-process integrity."
else
    WORKERS=${UVICORN_WORKERS:-4}
    echo "[INFO] Server mode: starting with ${WORKERS} workers."
fi

# 5. Launch FastAPI via Uvicorn with graceful signal forwarding
echo "=== Starting AgentStudio-RAG Uvicorn Server on 0.0.0.0:8000 ==="
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers "$WORKERS" \
    --proxy-headers \
    --forwarded-allow-ips='*'
