[CmdletBinding()]
param(
    [switch]$StartFast,
    [int]$TimeoutSeconds = 180,
    [string]$FastScript = 'E:\Llama.cpp\start-qwen3.6-agent.cmd'
)

# P0-20 最小启动器只操作项目 Compose 服务；Fast 模型仍交给用户指定的 Windows 脚本。
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = if ($env:AMR_PYTHON_EXE) { $env:AMR_PYTHON_EXE } else { 'E:\Anaconda\envs\torch128\python.exe' }

function Test-HttpOk {
    param([Parameter(Mandatory)][string]$Uri)

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 3
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    }
    catch {
        return $false
    }
}

function Wait-HttpOk {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][int]$Timeout
    )

    $deadline = (Get-Date).AddSeconds($Timeout)
    do {
        if (Test-HttpOk -Uri $Uri) {
            # 冒号紧邻变量时 PowerShell 会把它解析成变量名的一部分，显式边界避免启动脚本误报语法错误。
            Write-Host "[ok] ${Name}: $Uri"
            return
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "$Name 在 ${Timeout}s 内没有通过健康检查: $Uri"
}

Push-Location $projectRoot
try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "项目 Python 不存在: $python"
    }

    # 先确认 Docker Engine 可连接，再由 Compose 按 PostgreSQL/Qdrant/API 顺序启动。
    docker version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Engine 不可用；请先启动 Docker Desktop。'
    }
    docker compose -f .\compose.yaml up -d postgres qdrant api
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Compose 启动失败。'
    }

    Wait-HttpOk -Name 'Qdrant' -Uri 'http://127.0.0.1:6333/readyz' -Timeout $TimeoutSeconds
    Wait-HttpOk -Name 'API' -Uri 'http://127.0.0.1:8000/health' -Timeout $TimeoutSeconds

    & $python .\scripts\check_postgres.py
    if ($LASTEXITCODE -ne 0) {
        throw 'PostgreSQL 连通性检查失败。'
    }
    & $python .\scripts\check_qdrant.py
    if ($LASTEXITCODE -ne 0) {
        throw 'Qdrant 客户端检查失败。'
    }

    if ($StartFast) {
        # 不覆盖已有未知进程；用户必须先确认 8080 属于本项目 Fast 服务。
        $listener = Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue
        if ($listener) {
            throw '8080 已被占用；请先核对进程后再使用 -StartFast。'
        }
        if (-not (Test-Path -LiteralPath $FastScript -PathType Leaf)) {
            throw "Fast Windows 启动脚本不存在: $FastScript"
        }
        $fastWorkingDirectory = Split-Path -Parent $FastScript
        # 现有 .cmd 会在新窗口前台持有 llama-server；不改写脚本，也不启动 Smart。
        Start-Process -FilePath 'cmd.exe' `
            -ArgumentList @('/d', '/c', "call `"$FastScript`"") `
            -WorkingDirectory $fastWorkingDirectory `
            -WindowStyle Normal | Out-Null
        Wait-HttpOk -Name 'Qwen3.6 Fast' -Uri 'http://127.0.0.1:8080/health' -Timeout 300
        & $python .\scripts\check_model_gateway.py --profile fast
        if ($LASTEXITCODE -ne 0) {
            throw 'Fast 模型网关门禁失败。'
        }
    }

    docker compose -f .\compose.yaml ps
    Write-Host ''
    Write-Host '本地栈已启动：API http://127.0.0.1:8000/docs，Qdrant http://127.0.0.1:6333/dashboard。'
    if (-not $StartFast) {
        Write-Host '模型尚未启动；需要真实 PEVR 时另开窗口运行 E:\Llama.cpp\start-qwen3.6-agent.cmd。'
    }
}
finally {
    Pop-Location
}
