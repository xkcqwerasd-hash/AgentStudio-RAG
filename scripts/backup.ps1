# ==============================================================================
# AgentStudio-RAG — Data & Database Backup Script (PowerShell for Windows)
# ==============================================================================
# Usage:
#   .\scripts\backup.ps1 -Mode server -BackupRoot .\backups
# ==============================================================================
param (
    [string]$Mode = "server",
    [string]$BackupRoot = ".\backups"
)

$ErrorActionPreference = "Stop"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupDir = Join-Path $BackupRoot "${Timestamp}_${Mode}"

New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
Write-Host "=== Starting AgentStudio-RAG Backup ($Mode mode) ===" -ForegroundColor Cyan
Write-Host "Target Directory: $BackupDir"

if ($Mode -eq "server") {
    Write-Host "[1/2] Backing up PostgreSQL Database from container 'agentstudio-postgres'..."
    $dbFile = Join-Path $BackupDir "database.sql"
    docker compose exec -T postgres pg_dump -U agentstudio --clean --if-exists agentstudio_rag > $dbFile
    Write-Host "  -> Saved: $dbFile"

    Write-Host "[2/2] Backing up User Uploads & Attachments volume 'agentstudio_data'..."
    $absBackupDir = (Resolve-Path $BackupDir).Path
    docker run --rm -v agentstudio_data:/source:ro -v "${absBackupDir}:/backup" alpine tar -czf "/backup/uploads_data.tar.gz" -C /source .
    Write-Host "  -> Saved: $BackupDir\uploads_data.tar.gz"
} else {
    Write-Host "[1/2] Backing up SQLite Database..."
    if (Test-Path ".\data\storage\agentstudio_rag.db") {
        Copy-Item ".\data\storage\agentstudio_rag.db" -Destination (Join-Path $BackupDir "agentstudio_rag.db")
        Write-Host "  -> Saved: $BackupDir\agentstudio_rag.db"
    }

    Write-Host "[2/2] Backing up local .\data\uploads directory..."
    if (Test-Path ".\data\uploads") {
        Compress-Archive -Path ".\data\uploads\*" -DestinationPath (Join-Path $BackupDir "uploads_data.zip") -Force
        Write-Host "  -> Saved: $BackupDir\uploads_data.zip"
    }
}

$manifest = @{
    timestamp = $Timestamp
    mode = $Mode
    version = "1.3.0"
    created_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
} | ConvertTo-Json

$manifest | Out-File -FilePath (Join-Path $BackupDir "manifest.json") -Encoding utf8
Write-Host "=== Backup Completed Successfully ===" -ForegroundColor Green
