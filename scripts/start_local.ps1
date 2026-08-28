[CmdletBinding()]
param(
    [switch]$StartFast,
    [int]$TimeoutSeconds = 180,
    [string]$PythonExe = $env:AMR_PYTHON_EXE,
    [string]$FastArtifactRoot = $env:FAST_ARTIFACT_ROOT
)

# 本地覆盖会把数据库/Qdrant 仅发布到 loopback；正式 compose.yaml 不发布数据面端口。
# 所有密钥、路径和 Fast 文件哈希都在 Docker/进程状态改变前校验。
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$projectRoot = Split-Path -Parent $PSScriptRoot
$dotEnvPath = Join-Path $projectRoot '.env'
$secureFastScript = Join-Path $PSScriptRoot 'start_fast_secure.ps1'
$manifestPath = Join-Path $projectRoot 'config\fast_model_manifest.json'
$composeArguments = @(
    'compose',
    '-f', (Join-Path $projectRoot 'compose.yaml'),
    '-f', (Join-Path $projectRoot 'compose.dev.yaml')
)

function Get-ConfiguredValue {
    param([Parameter(Mandatory)][string]$Name)

    $processValue = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($processValue) {
        return $processValue
    }
    if (Test-Path -LiteralPath $dotEnvPath -PathType Leaf) {
        $pattern = '^' + [Regex]::Escape($Name) + '=(.*)$'
        foreach ($line in Get-Content -LiteralPath $dotEnvPath -Encoding UTF8) {
            if ($line -match $pattern) {
                return $Matches[1].Trim().Trim('"').Trim("'")
            }
        }
    }
    return ''
}

function Test-HttpOk {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [hashtable]$Headers = @{}
    )

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -Headers $Headers -TimeoutSec 3
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
        [Parameter(Mandatory)][int]$Timeout,
        [hashtable]$Headers = @{}
    )

    $deadline = (Get-Date).AddSeconds($Timeout)
    do {
        if (Test-HttpOk -Uri $Uri -Headers $Headers) {
            Write-Host "[ok] ${Name}: $Uri"
            return
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "$Name 在 ${Timeout}s 内没有通过健康检查: $Uri"
}

Push-Location $projectRoot
try {
    # 只加载白名单变量，避免把 .env 中无关内容注入当前 PowerShell 会话。
    $requiredSecrets = @{
        POSTGRES_PASSWORD = 16
        AMR_JWT_SECRET = 32
        AMR_HITL_SIGNING_SECRET = 32
        QDRANT_API_KEY = 32
        OPENAI_API_KEY = 32
    }
    foreach ($entry in $requiredSecrets.GetEnumerator()) {
        $value = Get-ConfiguredValue -Name $entry.Key
        if (-not $value -or $value.Length -lt $entry.Value -or $value -in @('123456', 'dummy')) {
            throw "$($entry.Key) 缺失、过短或仍是公开开发默认值；请先从 .env.example 创建 .env"
        }
        [Environment]::SetEnvironmentVariable($entry.Key, $value, 'Process')
    }

    if (-not $PythonExe) {
        $PythonExe = Get-ConfiguredValue -Name 'AMR_PYTHON_EXE'
    }
    if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        throw "AMR_PYTHON_EXE 未配置或文件不存在: $PythonExe"
    }
    if (-not $FastArtifactRoot) {
        $FastArtifactRoot = Get-ConfiguredValue -Name 'FAST_ARTIFACT_ROOT'
    }
    if (-not $FastArtifactRoot -or -not (Test-Path -LiteralPath $FastArtifactRoot -PathType Container)) {
        throw "FAST_ARTIFACT_ROOT 未配置或目录不存在: $FastArtifactRoot"
    }
    if (-not (Test-Path -LiteralPath $secureFastScript -PathType Leaf)) {
        throw "仓库 Fast 安全启动器不存在: $secureFastScript"
    }
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Fast artifact manifest 不存在: $manifestPath"
    }

    $env:AMR_PYTHON_EXE = $PythonExe
    $env:FAST_ARTIFACT_ROOT = (Resolve-Path -LiteralPath $FastArtifactRoot).Path
    $env:FAST_MODEL_PATH = Join-Path $env:FAST_ARTIFACT_ROOT 'models\Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ4_NL.gguf'
    $env:LLAMA_SERVER_PATH = Join-Path $env:FAST_ARTIFACT_ROOT 'llama-server.exe'
    $env:FAST_MODEL_VERIFY_ARTIFACT = 'true'
    $env:AMR_ENV = 'compose'
    if (-not $env:POSTGRES_DSN) {
        $postgresUser = Get-ConfiguredValue -Name 'POSTGRES_USER'
        if (-not $postgresUser) { $postgresUser = 'amr' }
        $postgresDatabase = Get-ConfiguredValue -Name 'POSTGRES_DB'
        if (-not $postgresDatabase) { $postgresDatabase = 'amr_agent' }
        # 发布示例要求 URL-safe 随机密码；这里仍做 URI 转义，避免特殊字符破坏主机检查 DSN。
        $encodedUser = [Uri]::EscapeDataString($postgresUser)
        $encodedPassword = [Uri]::EscapeDataString($env:POSTGRES_PASSWORD)
        $env:POSTGRES_DSN = "postgresql://${encodedUser}:${encodedPassword}@127.0.0.1:5432/$postgresDatabase"
    }

    $manifestPreview = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $verifyHashes = $true
    if ($null -ne $manifestPreview.verify_sha256) {
        $verifyHashes = [bool]$manifestPreview.verify_sha256
    }
    if ($verifyHashes) {
        Write-Host '[info] 正在校验 Fast 制品 SHA-256（19GB GGUF 可能需要数分钟）'
        & $PythonExe .\scripts\verify_fast_artifact.py --manifest $manifestPath `
            --model $env:FAST_MODEL_PATH --runtime $env:LLAMA_SERVER_PATH | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Fast artifact 校验失败。'
        }
    }
    else {
        # 演示启动跳过哈希扫描；仍要求 GGUF 与 llama-server 文件存在。
        Write-Host '[info] manifest.verify_sha256=false，跳过 Fast 制品 SHA-256'
        foreach ($requiredArtifact in @($env:FAST_MODEL_PATH, $env:LLAMA_SERVER_PATH)) {
            if (-not (Test-Path -LiteralPath $requiredArtifact -PathType Leaf)) {
                throw "Fast 制品不存在: $requiredArtifact"
            }
        }
    }
    & docker @composeArguments config --quiet
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Compose 配置/必填变量预检失败。'
    }
    docker version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Engine 不可用；请先启动 Docker Desktop。'
    }

    if ($StartFast) {
        $fastHeaders = @{ Authorization = "Bearer $($env:OPENAI_API_KEY)" }
        $listener = Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue
        if ($listener -and -not (Test-HttpOk -Uri 'http://127.0.0.1:8080/v1/models' -Headers $fastHeaders)) {
            throw '8080 已被未知或凭据不匹配的进程占用；未覆盖该进程。'
        }
        if (-not $listener) {
            # 后台启动器不需要交互窗口；进度输出已禁用，故障统一写固定 transcript。
            $shell = if (Get-Command pwsh.exe -ErrorAction SilentlyContinue) { 'pwsh.exe' } else { 'powershell.exe' }
            $fastProc = Start-Process -FilePath $shell `
                -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $secureFastScript) `
                -WorkingDirectory $projectRoot `
                -WindowStyle Hidden `
                -PassThru
            Write-Host "[info] Fast 启动器 PID=$($fastProc.Id)"
        }
        else {
            $fastProc = $null
        }
        Write-Host '[info] 等待 Qwen3.6 Fast /health（模型加载可能需要数分钟）'
        $fastDeadline = (Get-Date).AddSeconds(600)
        do {
            if ($fastProc -and $fastProc.HasExited) {
                $logPath = Join-Path $projectRoot 'tmp\fast_secure.transcript.log'
                $tail = ''
                if (Test-Path -LiteralPath $logPath) {
                    $tail = (Get-Content -LiteralPath $logPath -Tail 25 -ErrorAction SilentlyContinue) -join "`n"
                }
                throw "Fast 启动器已退出 $($fastProc.ExitCode)。日志：tmp\fast_secure.transcript.log`n$tail"
            }
            if (Test-HttpOk -Uri 'http://127.0.0.1:8080/health' -Headers $fastHeaders) {
                Write-Host '[ok] Qwen3.6 Fast: http://127.0.0.1:8080/health'
                break
            }
            Start-Sleep -Seconds 2
        } while ((Get-Date) -lt $fastDeadline)
        if (-not (Test-HttpOk -Uri 'http://127.0.0.1:8080/health' -Headers $fastHeaders)) {
            throw 'Qwen3.6 Fast 在 600s 内没有通过健康检查: http://127.0.0.1:8080/health'
        }
        & $PythonExe .\scripts\check_model_gateway.py --profile fast
        if ($LASTEXITCODE -ne 0) {
            throw 'Fast 模型网关门禁失败。'
        }
    }

    & docker @composeArguments up -d postgres qdrant api
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Compose 启动失败。'
    }

    $qdrantHeaders = @{ 'api-key' = $env:QDRANT_API_KEY }
    Wait-HttpOk -Name 'Qdrant' -Uri 'http://127.0.0.1:6333/readyz' `
        -Headers $qdrantHeaders -Timeout $TimeoutSeconds
    Wait-HttpOk -Name 'API' -Uri 'http://127.0.0.1:8000/health' -Timeout $TimeoutSeconds

    & $PythonExe .\scripts\check_postgres.py
    if ($LASTEXITCODE -ne 0) {
        throw 'PostgreSQL 连通性检查失败。'
    }
    & $PythonExe .\scripts\check_qdrant.py
    if ($LASTEXITCODE -ne 0) {
        throw 'Qdrant 客户端检查失败。'
    }

    & docker @composeArguments ps
    Write-Host ''
    Write-Host '本地开发栈已启动：API http://127.0.0.1:8000/docs；数据库和 Qdrant 仅绑定 loopback。'
    if (-not $StartFast) {
        Write-Host '模型尚未启动；需要真实 PEVR 时使用 -StartFast，由安全启动器执行制品/密钥门禁。'
    }
}
finally {
    Pop-Location
}
