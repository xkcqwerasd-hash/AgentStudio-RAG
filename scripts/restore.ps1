# ==============================================================================
# AgentStudio-RAG — Data & Database Restoration Script (PowerShell for Windows)
# ==============================================================================
# Usage:
#   .\scripts\restore.ps1 -BackupDir .\backups\20261104_120000_server -Mode server
# ==============================================================================
param (
    [Parameter(Mandatory=$true)]
    [string]$BackupDir,
    [string]$Mode = "server"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $BackupDir)) {
    Write-Error "Backup directory '$BackupDir' does not exist."
    exit 1
}

Write-Host "=== Starting AgentStudio-RAG Restore ($Mode mode) ===" -ForegroundColor Cyan
Write-Host "Source Backup: $BackupDir"

if ($Mode -eq "server") {
    $dbFile = Join-Path $BackupDir "database.sql"
    if (-not (Test-Path $dbFile)) {
        Write-Error "Missing $dbFile"
        exit 1
    }
    Write-Host "[1/2] Restoring PostgreSQL Database to container 'agentstudio-postgres'..."
    Get-Content $dbFile | docker compose exec -T postgres psql -U agentstudio agentstudio_rag
    Write-Host "  -> PostgreSQL database restored successfully."

    $uploadsTar = Join-Path $BackupDir "uploads_data.tar.gz"
    if (Test-Path $uploadsTar) {
        Write-Host "[2/2] Restoring User Uploads volume 'agentstudio_data'..."
        $absBackupDir = (Resolve-Path $BackupDir).Path
        docker run --rm -v agentstudio_data:/target -v "${absBackupDir}:/backup:ro" alpine sh -c "rm -rf /target/* && tar -xzf /backup/uploads_data.tar.gz -C /target"
        Write-Host "  -> Volume 'agentstudio_data' restored successfully."
    }
} else {
    Write-Host "[1/2] Restoring SQLite Database..."
    New-Item -ItemType Directory -Path ".\data\storage", ".\data\uploads" -Force | Out-Null
    $sqliteFile = Join-Path $BackupDir "agentstudio_rag.db"
    if (Test-Path $sqliteFile) {
        Copy-Item $sqliteFile -Destination ".\data\storage\agentstudio_rag.db" -Force
        Write-Host "  -> SQLite database restored to .\data\storage\agentstudio_rag.db"
    }

    $zipFile = Join-Path $BackupDir "uploads_data.zip"
    if (Test-Path $zipFile) {
        Write-Host "[2/2] Restoring .\data\uploads directory..."
        Expand-Archive -Path $zipFile -DestinationPath ".\data\uploads" -Force
        Write-Host "  -> Uploads directory restored to .\data\uploads"
    }
}

Write-Host "=== Restore Completed Successfully ===" -ForegroundColor Green
