#!/bin/sh
set -e

# ==============================================================================
# AgentStudio-RAG — Data & Database Restoration Script
# ==============================================================================
# Usage:
#   ./scripts/restore.sh <backup_dir> [server|local]
# ==============================================================================

BACKUP_DIR=$1
MODE=${2:-server}

if [ -z "$BACKUP_DIR" ] || [ ! -d "$BACKUP_DIR" ]; then
    echo "[ERROR] Backup directory not found or not specified."
    echo "Usage: ./scripts/restore.sh <path_to_backup_directory> [server|local]"
    exit 1
fi

echo "=== Starting AgentStudio-RAG Restore (${MODE} mode) ==="
echo "Source Backup: ${BACKUP_DIR}"

if [ "$MODE" = "server" ]; then
    if [ ! -f "${BACKUP_DIR}/database.sql.gz" ]; then
        echo "[FATAL ERROR] ${BACKUP_DIR}/database.sql.gz does not exist."
        exit 1
    fi

    echo "[1/2] Restoring PostgreSQL Database to container 'agentstudio-postgres'..."
    gunzip -c "${BACKUP_DIR}/database.sql.gz" | docker compose exec -T postgres psql -U "${POSTGRES_USER:-agentstudio}" "${POSTGRES_DB:-agentstudio_rag}"
    echo "  -> PostgreSQL database restored successfully."

    if [ -f "${BACKUP_DIR}/uploads_data.tar.gz" ]; then
        echo "[2/2] Restoring User Uploads volume 'agentstudio_data'..."
        docker run --rm -v agentstudio_data:/target -v "$(pwd)/${BACKUP_DIR}:/backup:ro" alpine \
            sh -c "rm -rf /target/* && tar -xzf /backup/uploads_data.tar.gz -C /target"
        echo "  -> Volume 'agentstudio_data' restored successfully."
    fi

else
    echo "[1/2] Restoring SQLite Database..."
    mkdir -p ./data/storage ./data/uploads
    if [ -f "${BACKUP_DIR}/agentstudio_rag.db.gz" ]; then
        gunzip -c "${BACKUP_DIR}/agentstudio_rag.db.gz" > "./data/storage/agentstudio_rag.db"
        echo "  -> SQLite database restored to ./data/storage/agentstudio_rag.db"
    elif [ -f "${BACKUP_DIR}/agentstudio_rag.db" ]; then
        cp "${BACKUP_DIR}/agentstudio_rag.db" "./data/storage/agentstudio_rag.db"
        echo "  -> SQLite database restored to ./data/storage/agentstudio_rag.db"
    fi

    if [ -f "${BACKUP_DIR}/uploads_data.tar.gz" ]; then
        echo "[2/2] Restoring ./data/uploads directory..."
        tar -xzf "${BACKUP_DIR}/uploads_data.tar.gz" -C ./data/uploads
        echo "  -> Uploads directory restored to ./data/uploads"
    fi
fi

echo "=== Restore Completed Successfully ==="
