@echo off
chcp 65001 >nul
REM wan-repro dev launcher (full stack, idempotent):
REM   1. 引擎代理 bridge :8776 (python ui/server.py)
REM   2. 画布 Agent :17371 (仓库内 tsx 开发版, 禁止 npx 发布版)
REM   3. vite 主窗口 :3000 (autoStartBridge 复用/拉起 :8779)
setlocal

REM ---------- 1. 引擎代理 :8776 ----------
call :pick_python pyexe
if not defined pyexe (
  echo [x] 未找到 python/python3/py -3, 请先安装 Python.
  pause
  exit /b 1
)
netstat -ano | findstr ":8776" | findstr "LISTENING" >nul 2>nul
if errorlevel 1 (
  echo [启动] 正在拉起 引擎代理 :8776 ...
  start "引擎代理 :8776" cmd /k "cd /d %~dp0wanrepro && %pyexe% ui\server.py --port 8776 --no-browser"
) else (
  echo [复用] 引擎代理 :8776 已监听, 跳过.
)

REM ---------- 2. 画布 Agent :17371 ----------
REM 注意: 必须用仓库内开发版 (npm run dev => tsx src/index.ts),
REM 绝不能用 npx 拉的 npm 发布版 - 那版没有新契约, 曾因此出现双实例竞争。
netstat -ano | findstr ":17371" | findstr "LISTENING" >nul 2>nul
if errorlevel 1 (
  echo [启动] 正在拉起 画布 Agent :17371 ...
  start "画布 Agent :17371" cmd /k "cd /d %~dp0canvas-agent && npm run dev"
) else (
  echo [复用] 画布 Agent :17371 已监听, 跳过.
)

REM ---------- 3. vite 主窗口 :3000 ----------
cd /d "%~dp0web"
where npm >nul 2>nul
if errorlevel 1 (
  echo [x] npm not found in PATH. Install Node.js first.
  pause
  exit /b 1
)
netstat -ano | findstr ":3000" | findstr "LISTENING" >nul 2>nul
if errorlevel 1 (
  echo [启动] 正在拉起 vite 主窗口 http://localhost:3000
  start "" http://localhost:3000
  call npm run dev
) else (
  echo [复用] vite 主窗口 :3000 已监听, 跳过启动.
  start "" http://localhost:3000
)
pause
exit /b 0

:pick_python
REM 探测顺序与 vite.config.ts autoStartBridge 插件一致: python3 -> python -> py -3
set "%~1="
for %%C in (python3 python "py -3") do (
  where %%~C >nul 2>nul && (
    set "%~1=%%~C"
    goto :eof
  )
)
goto :eof
