[CmdletBinding()]
param(
    [switch]$RotateRunningPostgres,
    [string]$FastArtifactRoot = 'E:\Llama.cpp',
    [string]$PythonExe = 'E:\Anaconda\envs\torch128\python.exe'
)

# 该脚本只把随机值写入 gitignore 的 .env；控制台、Trace 和版本文件均不输出明文。
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $projectRoot '.env'
if (Test-Path -LiteralPath $envPath) {
    throw ".env 已存在，未覆盖现有凭据: $envPath"
}
if (-not (Test-Path -LiteralPath $FastArtifactRoot -PathType Container)) {
    throw "Fast artifact 目录不存在: $FastArtifactRoot"
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python 解释器不存在: $PythonExe"
}

function New-UrlSafeSecret {
    param([int]$ByteCount = 36)

    $bytes = [byte[]]::new($ByteCount)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

$postgresPassword = New-UrlSafeSecret
$jwtSecret = New-UrlSafeSecret
$hitlSecret = New-UrlSafeSecret
$qdrantKey = New-UrlSafeSecret
$fastKey = New-UrlSafeSecret
$lines = @(
    'AMR_ENV=development',
    'POSTGRES_USER=amr',
    "POSTGRES_PASSWORD=$postgresPassword",
    'POSTGRES_DB=amr_agent',
    "POSTGRES_DSN=postgresql://amr:${postgresPassword}@127.0.0.1:5432/amr_agent",
    "AMR_JWT_SECRET=$jwtSecret",
    "AMR_HITL_SIGNING_SECRET=$hitlSecret",
    "QDRANT_API_KEY=$qdrantKey",
    "OPENAI_API_KEY=$fastKey",
    'OPENAI_BASE_URL=http://127.0.0.1:8080/v1',
    'LLM_PROFILE=fast',
    'LLM_MODEL=qwen3.6-fast',
    'MODEL_GATEWAY_VALIDATE_ON_STARTUP=false',
    "FAST_ARTIFACT_ROOT=$FastArtifactRoot",
    "AMR_PYTHON_EXE=$PythonExe",
    'FAST_MODEL_VERIFY_ARTIFACT=true',
    'QDRANT_URL=http://127.0.0.1:6333'
)
[IO.File]::WriteAllLines($envPath, $lines, [Text.UTF8Encoding]::new($false))

if ($RotateRunningPostgres) {
    $running = docker inspect -f '{{.State.Running}}' amr-postgres 2>$null
    if ($LASTEXITCODE -eq 0 -and $running -eq 'true') {
        # 密码来自 URL-safe 字符集；仍做 SQL literal 转义，避免以后调整生成策略时埋下注入点。
        $escaped = $postgresPassword.Replace("'", "''")
        "ALTER ROLE amr WITH PASSWORD '$escaped';" | docker exec -i amr-postgres psql -v ON_ERROR_STOP=1 -U amr -d postgres | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw '现有 PostgreSQL 角色密码轮换失败；.env 已生成但尚不可用于旧 volume。'
        }
    }
}

Write-Host '[ok] 已生成 .env；五个服务凭据彼此独立且未输出明文。'
if (-not $RotateRunningPostgres) {
    Write-Host '若复用已由旧密码初始化的 postgres_data，请显式使用 -RotateRunningPostgres。'
}
