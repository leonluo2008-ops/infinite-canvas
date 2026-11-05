# wan-repro 整合包 · 服务管理器（由 启动整合包.bat 以 Bypass 调起）
$ErrorActionPreference = "Continue"
$Bundle = Split-Path -Parent $MyInvocation.MyCommand.Path
$PyExe = Join-Path $Bundle "runtime\python\python.exe"
$Srv = Join-Path $Bundle "app\ui\server.py"
$TokenFile = Join-Path $Bundle "config\token"
$LogDir = Join-Path $Bundle "logs"
$Port = 8776

if (-not (Test-Path $PyExe)) { Write-Host "[x] 缺少运行时：$PyExe（整合包不完整，请联系交付方）"; [Console]::In.ReadLine() | Out-Null; exit 1 }
if (-not (Test-Path $Srv))   { Write-Host "[x] 缺少服务程序：$Srv"; [Console]::In.ReadLine() | Out-Null; exit 1 }

function Get-SrvPid {
    $line = netstat -ano | Select-String ":$Port\s.*LISTENING" | Select-Object -First 1
    if ($line) { ($line -split '\s+')[-1] } else { 0 }
}

function Show-Menu {
    Clear-Host
    $pid2 = Get-SrvPid
    $state = if ($pid2 -ne 0) { "运行中（PID $pid2）" } else { "未运行" }
    Write-Host "============================================================"
    Write-Host "   wan-repro 整合包 · 服务管理            v1.0.0"
    Write-Host "============================================================"
    Write-Host "   服务状态   :  $state"
    Write-Host ""
    Write-Host "   本机访问   :  http://127.0.0.1:$Port   （免口令）"
    $lan = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' } |
        Select-Object -First 1).IPAddress
    if ($lan) {
        if (Test-Path $TokenFile) {
            $tok = (Get-Content $TokenFile -First 1).Trim()
            Write-Host "   局域网访问 :  http://$lan`:$Port/?token=$tok"
            Write-Host "                （发给同一网络的其他人用）"
        } else {
            Write-Host "   局域网访问 :  http://$lan`:$Port"
        }
    }
    Write-Host "   公网访问   :  需另配 Cloudflare 隧道（见 docs\DEPLOY-GUIDE.md）"
    Write-Host "   Hermes Agent:  画布内 Agent 配置连接地址（见 docs\DEPLOY-GUIDE.md）"
    Write-Host "------------------------------------------------------------"
    Write-Host "  [1] 启动服务    [2] 停止服务    [3] 重启服务"
    Write-Host "  [4] 刷新状态    [5] 修改口令    [6] 打开画布"
    Write-Host "  [7] 运行日志    [q] 退出"
    Write-Host "------------------------------------------------------------"
}

function Start-Srv {
    if ((Get-SrvPid) -ne 0) { Write-Host "[=] 服务已在运行"; Start-Sleep 1; return }
    Write-Host "[..] 正在启动服务（端口 $Port，后台运行无窗口）..."
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
    Start-Process -FilePath $PyExe -ArgumentList "`"$Srv`"","--host","0.0.0.0","--port","$Port","--no-browser" `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "bridge.log") `
        -RedirectStandardError (Join-Path $LogDir "bridge.err.log")
    $ok = $false
    foreach ($i in 1..15) {
        Start-Sleep 1
        if ((Get-SrvPid) -ne 0) { $ok = $true; break }
    }
    if ($ok) { Write-Host "[√] 服务已启动：http://127.0.0.1:$Port" }
    else     { Write-Host "[x] 15 秒内端口未就绪，请选 [7] 看日志或检查端口占用" }
    Start-Sleep 1
}

function Stop-Srv {
    $p = Get-SrvPid
    if ($p -eq 0) { Write-Host "[=] 服务未在运行"; Start-Sleep 1; return }
    Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
    Write-Host "[√] 已停止服务（PID $p）"
    Start-Sleep 1
}

function Set-Token {
    Write-Host "当前口令文件: $TokenFile"
    $new = [Console]::In.ReadLine()
    if (-not $new) { $new = ([Guid]::NewGuid().ToString('N').Substring(0,12)) }
    Set-Content -Path $TokenFile -Value $new -Encoding ASCII
    Write-Host "[√] 口令已更新：$new（重启服务后对外生效，选 [3]）"
    [Console]::In.ReadLine() | Out-Null
}

function Show-Log {
    Write-Host "---------- 最近 60 行运行日志 ----------"
    foreach ($f in @("bridge.err.log", "bridge.log")) {
        $log = Join-Path $LogDir $f
        if (Test-Path $log) { Get-Content $log -Tail 60 }
    }
    if (-not (Test-Path (Join-Path $LogDir "bridge.log"))) {
        Write-Host "暂无日志——服务还没启动过（先选 [1]）"
    }
    Write-Host "----------------------------------------"
    [Console]::In.ReadLine() | Out-Null
}

while ($true) {
    Show-Menu
    $sel = [Console]::In.ReadLine()
    switch ($sel) {
        "1" { Start-Srv }
        "2" { Stop-Srv }
        "3" { Stop-Srv; Start-Sleep 1; Start-Srv }
        "4" { continue }
        "5" { Set-Token }
        "6" { Start-Process "http://127.0.0.1:$Port" }
        "7" { Show-Log }
        "q" { exit 0 }
        "Q" { exit 0 }
        default { continue }
    }
}
