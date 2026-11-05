@echo off
chcp 65001 >nul
echo ======================================================
echo    Starting AgentStudio-RAG Development Environment
echo ======================================================

cd /d "%~dp0"

echo [1/2] Starting Backend (FastAPI on http://127.0.0.1:8000)...
start "AgentStudio-RAG Backend" cmd /k "cd backend && .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload"

echo [2/2] Starting Frontend (Vite on http://127.0.0.1:5173)...
start "AgentStudio-RAG Frontend" cmd /k "cd frontend && npm run dev"

echo.
echo Both services are launching in separate windows!
echo Backend Docs: http://127.0.0.1:8000/docs
echo Frontend Web: http://127.0.0.1:5173
echo ======================================================
