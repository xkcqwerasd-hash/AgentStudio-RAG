#!/bin/sh
set -e

# ==============================================================================
# AgentStudio-RAG — Data & Database Backup Script
# ==============================================================================
# Usage:
#   ./scripts/backup.sh [server|local] [output_dir]
# ==============================================================================

MODE=${1:-server}
BACKUP_ROOT=${2:-./backups}
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_DIR="${BACKUP_ROOT}/${TIMESTAMP}_${MODE}"

mkdir -p "$BACKUP_DIR"
echo "=== Starting AgentStudio-RAG Backup (${MODE} mode) ==="
echo "Target Directory: ${BACKUP_DIR}"

if [ "$MODE" = "server" ]; then
    echo "[1/2] Backing up PostgreSQL Database from container 'agentstudio-postgres'..."
    docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-agentstudio}" --clean --if-exists "${POSTGRES_DB:-agentstudio_rag}" | gzip > "${BACKUP_DIR}/database.sql.gz"
    echo "  -> Saved: ${BACKUP_DIR}/database.sql.gz ($(du -h "${BACKUP_DIR}/database.sql.gz" | cut -f1))"

    echo "[2/2] Backing up User Uploads & Attachments volume 'agentstudio_data'..."
    docker run --rm -v agentstudio_data:/source:ro -v "$(pwd)/${BACKUP_DIR}:/backup" alpine \
        tar -czf "/backup/uploads_data.tar.gz" -C /source .
    echo "  -> Saved: ${BACKUP_DIR}/uploads_data.tar.gz ($(du -h "${BACKUP_DIR}/uploads_data.tar.gz" | cut -f1))"

else
    echo "[1/2] Backing up SQLite Database..."
    if [ -f "./data/storage/agentstudio_rag.db" ]; then
        cp "./data/storage/agentstudio_rag.db" "${BACKUP_DIR}/agentstudio_rag.db"
        gzip -k "${BACKUP_DIR}/agentstudio_rag.db"
        echo "  -> Saved: ${BACKUP_DIR}/agentstudio_rag.db.gz"
    else
        echo "  -> [WARNING] SQLite file ./data/storage/agentstudio_rag.db not found."
    fi

    echo "[2/2] Backing up local ./data/uploads directory..."
    if [ -d "./data/uploads" ]; then
        tar -czf "${BACKUP_DIR}/uploads_data.tar.gz" -C ./data/uploads .
        echo "  -> Saved: ${BACKUP_DIR}/uploads_data.tar.gz"
    fi
fi

# Write metadata manifest
cat <<EOF > "${BACKUP_DIR}/manifest.json"
{
  "timestamp": "${TIMESTAMP}",
  "mode": "${MODE}",
  "version": "1.3.0",
  "created_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}
EOF

echo "=== Backup Completed Successfully ==="
echo "Manifest written to: ${BACKUP_DIR}/manifest.json"
