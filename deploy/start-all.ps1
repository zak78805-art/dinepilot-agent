# 一键启动商家经营 Agent 全链路：Redis -> Java(8080) -> Agent(8090) -> Nginx(80)
# 用法（在本文件所在目录）：powershell -ExecutionPolicy Bypass -File .\start-all.ps1
# 说明：DSH/非交互进程里可能读不到 User 级环境变量，本脚本显式从注册表回填，避免
#       DASHSCOPE_API_KEY 变成空字符串导致 Agent 抛 RuntimeError 并被路由兜底成 502。

$ErrorActionPreference = 'Stop'

$JavaJar   = 'D:\后端初始工程\sky-server\target\sky-server-1.0-SNAPSHOT.jar'
$AgentDir  = 'D:\sky-agent'
$AgentPy   = 'D:\sky-agent\.venv\Scripts\python.exe'
$NginxDir  = 'D:\qianduan\shuhongfan sky-take-out main\nginx-1.20.2'
$RedisName = 'sky-redis'

function Import-UserEnv([string[]]$Names) {
    foreach ($name in $Names) {
        $value = [Environment]::GetEnvironmentVariable($name, 'User')
        if ($value) { Set-Item -Path "Env:$name" -Value $value }
    }
}

function Wait-Port([int]$Port, [int]$Seconds = 60) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue) {
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

Import-UserEnv 'SKY_DB_USERNAME', 'SKY_DB_PASSWORD', 'DASHSCOPE_API_KEY', 'DASHSCOPE_BASE_URL'

# 1) Redis（Docker 容器 sky-redis）
if (Test-NetConnection -ComputerName 127.0.0.1 -Port 6379 -InformationLevel Quiet -WarningAction SilentlyContinue) {
    Write-Host '[1/4] Redis 6379 已在线'
} else {
    Write-Host '[1/4] 启动 Redis 容器...'
    docker start $RedisName | Out-Null
    if (-not (Wait-Port 6379 60)) { throw 'Redis 未能在 6379 监听，请确认 Docker Desktop 已启动' }
}

# 2) Java 后端（8080）
if (Wait-Port 8080 1) {
    Write-Host '[2/4] Java 8080 已在线'
} else {
    Write-Host '[2/4] 启动 Java 后端...'
    $log = Join-Path $AgentDir 'logs\java-backend.log'
    Start-Process -FilePath 'java' -ArgumentList '-jar', "`"$JavaJar`"" `
        -WorkingDirectory 'D:\后端初始工程' -WindowStyle Hidden `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    if (-not (Wait-Port 8080 120)) { throw "Java 未能在 8080 监听，查看 $log" }
}

# 3) Python Agent（8090）
if (Wait-Port 8090 1) {
    Write-Host '[3/4] Agent 8090 已在线'
} else {
    if (-not $env:DASHSCOPE_API_KEY) { throw 'DASHSCOPE_API_KEY 为空，Agent 无法调用模型' }
    Write-Host '[3/4] 启动 Python Agent...'
    $log = Join-Path $AgentDir 'logs\agent-8090.log'
    Start-Process -FilePath $AgentPy `
        -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8090' `
        -WorkingDirectory $AgentDir -WindowStyle Hidden `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    if (-not (Wait-Port 8090 90)) { throw "Agent 未能在 8090 监听，查看 $log" }
}

# 4) Nginx（80）
if (Wait-Port 80 1) {
    Write-Host '[4/4] Nginx 80 已在线'
} else {
    Write-Host '[4/4] 启动 Nginx...'
    Push-Location $NginxDir
    try {
        & .\nginx.exe -t
        if ($LASTEXITCODE -ne 0) { throw 'nginx.conf 语法检查未通过' }
        Start-Process -FilePath '.\nginx.exe' -WorkingDirectory $NginxDir -WindowStyle Hidden
    } finally {
        Pop-Location
    }
    if (-not (Wait-Port 80 30)) { throw 'Nginx 未能在 80 监听' }
}

Write-Host ''
Write-Host '全部就绪：'
Write-Host '  管理端      http://localhost/'
Write-Host '  经营助手    http://localhost/assistant/'
Write-Host '  健康检查    http://localhost/assistant-api/health'
Write-Host ''
Write-Host '注意：先在管理端用 admin / 123456 登录，助手页读取同源 Cookie 的 token。'
