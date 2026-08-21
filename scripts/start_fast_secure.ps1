[CmdletBinding()]
param(
    [string]$ManifestPath = '',
    [string]$ModelPath = '',
    [string]$ServerPath = '',
    [string]$PythonExe = $env:AMR_PYTHON_EXE
)

# 发布启动器直接调用 llama-server，避免继承外部旧脚本的开放 CORS、无鉴权和弹窗行为。
# API key 只通过进程环境传递，不写入命令行、日志、manifest 或 Trace。
# 隐藏窗口里的 Invoke-WebRequest 进度条会把启动器卡死，必须关掉进度输出。
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$projectRoot = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $projectRoot 'tmp'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$transcriptPath = Join-Path $logDir 'fast_secure.transcript.log'
Start-Transcript -Path $transcriptPath -Force | Out-Null
if (-not $ManifestPath) {
    $ManifestPath = Join-Path $projectRoot 'config\fast_model_manifest.json'
}

function Resolve-ArtifactPath {
    param(
        [Parameter(Mandatory)][string]$RecordedPath,
        [string]$OverridePath = ''
    )

    $candidate = if ($OverridePath) { $OverridePath } else { $RecordedPath }
    if ([System.IO.Path]::IsPathRooted($candidate)) {
        return [System.IO.Path]::GetFullPath($candidate)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $projectRoot $candidate))
}

function Assert-ArtifactPresent {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][long]$ExpectedSize,
        [Parameter(Mandatory)][string]$ExpectedSha256,
        [Parameter(Mandatory)][bool]$VerifySha256
    )

    # 路径和大小是瞬时 stat；SHA-256 才是 19GB 扫描。演示启动按 manifest 关闭哈希。
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Name 不存在: $Path"
    }
    $item = Get-Item -LiteralPath $Path
    if ($item.Length -ne $ExpectedSize) {
        throw "$Name 大小与 manifest 不一致: $($item.Length) != $ExpectedSize"
    }
    if ($VerifySha256) {
        $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash
        if ($actualHash -ne $ExpectedSha256.ToUpperInvariant()) {
            throw "$Name SHA-256 与 manifest 不一致"
        }
    }
}

if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Fast artifact manifest 不存在: $ManifestPath"
}
$proxyModule = Join-Path $projectRoot 'services\model_gateway\secure_proxy.py'
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "AMR_PYTHON_EXE 未配置或文件不存在: $PythonExe"
}
if (-not (Test-Path -LiteralPath $proxyModule -PathType Leaf)) {
    throw "Fast 安全代理模块不存在: $proxyModule"
}
$manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json

# 这些值是 P0 发布身份的一部分；manifest 即使被误改，也不能把启动器悄悄放宽成
# 多槽、思考模式或其他量化配置。SHA-256 由 manifest.verify_sha256 决定是否扫描。
if (
    $manifest.schema_version -ne 'amr.fast-model-manifest.v1' -or
    $manifest.profile -ne 'fast' -or
    $manifest.alias -ne 'qwen3.6-fast' -or
    $manifest.quantization -ne 'IQ4_NL' -or
    $manifest.context_window -ne 16384 -or
    [double]$manifest.temperature -ne 0.1 -or
    [double]$manifest.top_p -ne 0.95 -or
    $manifest.top_k -ne 20 -or
    $manifest.parallel_slots -ne 1 -or
    $manifest.reasoning_enabled -ne $false
) {
    throw 'Fast artifact manifest 的发布身份或运行参数不符合 P0 固定契约'
}

$modelOverride = if ($ModelPath) { $ModelPath } else { $env:FAST_MODEL_PATH }
$serverOverride = if ($ServerPath) { $ServerPath } else { $env:LLAMA_SERVER_PATH }
$resolvedModel = Resolve-ArtifactPath -RecordedPath $manifest.model.path -OverridePath $modelOverride
$resolvedServer = Resolve-ArtifactPath -RecordedPath $manifest.runtime_binary.path -OverridePath $serverOverride
$resolvedLauncher = Resolve-ArtifactPath -RecordedPath $manifest.launch_script.path
$verifyHashes = $true
if ($null -ne $manifest.verify_sha256) {
    $verifyHashes = [bool]$manifest.verify_sha256
}
if (-not $verifyHashes) {
    Write-Host '[info] manifest.verify_sha256=false，跳过 Fast 制品 SHA-256'
}

Assert-ArtifactPresent -Name 'Fast GGUF' -Path $resolvedModel `
    -ExpectedSize $manifest.model.size_bytes -ExpectedSha256 $manifest.model.sha256 `
    -VerifySha256 $verifyHashes
Assert-ArtifactPresent -Name 'llama-server' -Path $resolvedServer `
    -ExpectedSize $manifest.runtime_binary.size_bytes -ExpectedSha256 $manifest.runtime_binary.sha256 `
    -VerifySha256 $verifyHashes
Assert-ArtifactPresent -Name 'Fast 启动器' -Path $resolvedLauncher `
    -ExpectedSize $manifest.launch_script.size_bytes -ExpectedSha256 $manifest.launch_script.sha256 `
    -VerifySha256 $verifyHashes

$apiKey = if ($env:FAST_MODEL_API_KEY) { $env:FAST_MODEL_API_KEY } else { $env:OPENAI_API_KEY }
if (-not $apiKey -or $apiKey.Length -lt 32 -or $apiKey -eq 'dummy') {
    throw '必须通过 FAST_MODEL_API_KEY 或 OPENAI_API_KEY 注入至少 32 字符的 Fast API key'
}

$previousLlamaApiKey = $env:LLAMA_API_KEY
$backendProcess = $null
try {
    $env:LLAMA_API_KEY = $apiKey
    # Start-Process -ArgumentList 会把额外引号算进 argv；模型路径必须是单独一个参数。
    $backendArguments = @(
        '--model', $resolvedModel,
        '--no-mmproj',
        '--alias', $manifest.alias,
        '--host', '127.0.0.1',
        '--port', '18080',
        '--cors-origins', 'localhost',
        '--cors-methods', 'GET,POST,OPTIONS',
        '--cors-headers', 'Authorization,Content-Type',
        '--no-cors-credentials',
        '--no-agent',
        '--no-ui-mcp-proxy',
        '--no-webui',
        '--reasoning', 'off',
        '--gpu-layers', 'all',
        '--fit', 'off',
        '--n-cpu-moe', '12',
        '--load-mode', 'none',
        '--ctx-size', [string]$manifest.context_window,
        '--parallel', [string]$manifest.parallel_slots,
        '--threads', '14',
        '--threads-batch', '20',
        '--temp', [string]$manifest.temperature,
        '--top-p', [string]$manifest.top_p,
        '--top-k', [string]$manifest.top_k,
        '--metrics',
        '--flash-attn', 'on',
        '--cache-type-k', 'q8_0',
        '--cache-type-v', 'q8_0'
    )
    $stdoutLog = Join-Path $logDir 'llama-server.out.log'
    $stderrLog = Join-Path $logDir 'llama-server.err.log'
    Write-Host "[info] 启动 llama-server：$resolvedServer"
    # 重定向标准输出，避免 Hidden 控制台进度条死锁；不要再用 WindowStyle Hidden。
    $backendProcess = Start-Process -FilePath $resolvedServer `
        -ArgumentList $backendArguments `
        -WorkingDirectory (Split-Path -Parent $resolvedServer) `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru
    Write-Host "[info] llama-server PID=$($backendProcess.Id)，等待 18080/health"
    $deadline = (Get-Date).AddMinutes(5)
    do {
        if ($backendProcess.HasExited) {
            $stderrTail = ''
            if (Test-Path -LiteralPath $stderrLog) {
                $stderrTail = (Get-Content -LiteralPath $stderrLog -Tail 30 -ErrorAction SilentlyContinue) -join "`n"
            }
            throw "llama-server 在 ready 前退出: $($backendProcess.ExitCode)`n$stderrTail"
        }
        try {
            $ready = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18080/health' `
                -Headers @{ Authorization = "Bearer $apiKey" } -TimeoutSec 2
            if ($ready.StatusCode -eq 200) { break }
        }
        catch {
            Start-Sleep -Seconds 1
        }
    } while ((Get-Date) -lt $deadline)
    if ((Get-Date) -ge $deadline) {
        throw 'llama-server 后端在 5 分钟内没有 ready'
    }
    Write-Host '[ok] llama-server /health'

    $env:FAST_MODEL_BACKEND_URL = 'http://127.0.0.1:18080'
    & $PythonExe -m services.model_gateway.secure_proxy
    $proxyExit = $LASTEXITCODE
}
finally {
    $env:LLAMA_API_KEY = $previousLlamaApiKey
    if ($backendProcess -and -not $backendProcess.HasExited) {
        # 只停止本脚本精确记录的子进程，不按名称清理其他模型服务。
        Stop-Process -Id $backendProcess.Id -Force
    }
    Stop-Transcript | Out-Null
}
exit $proxyExit
