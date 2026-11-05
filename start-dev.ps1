# Start AgentStudio-RAG Development Environment
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "   Starting AgentStudio-RAG Development Environment" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1. Start Backend
Write-Host "[1/2] Starting Backend (FastAPI on http://127.0.0.1:8000)..." -ForegroundColor Green
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$Root\backend'; .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload"

# 2. Start Frontend
Write-Host "[2/2] Starting Frontend (Vite on http://127.0.0.1:5173)..." -ForegroundColor Green
Start-Process cmd -ArgumentList "/k", "cd '$Root\frontend' && npm run dev"

Write-Host ""
Write-Host "Both services launched successfully!" -ForegroundColor Yellow
Write-Host "Backend API & Swagger Docs: http://127.0.0.1:8000/docs" -ForegroundColor White
Write-Host "Frontend Web Interface:    http://127.0.0.1:5173" -ForegroundColor White
Write-Host "======================================================" -ForegroundColor Cyan
