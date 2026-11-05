@echo off
REM wan-repro dev launcher: double-click = npm run dev (vite :3000 + auto bridge :8779)
setlocal
cd /d "%~dp0web"
where npm >nul 2>nul
if errorlevel 1 (
  echo [x] npm not found in PATH. Install Node.js first.
  pause
  exit /b 1
)
echo [*] Starting dev server at http://localhost:3000
start "" http://localhost:3000
call npm run dev
pause
