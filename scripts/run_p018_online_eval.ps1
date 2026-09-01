[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$OutputDir = 'tmp\p018_online_eval',
    [double]$VerificationTimeout = 120.0,
    [switch]$MeasureTtft
)

# 在线 60 例必须走真实 Fast；本脚本不启动模型，启动失败由 Python 门禁报出。
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = if ($env:AMR_PYTHON_EXE) { $env:AMR_PYTHON_EXE } else { 'E:\Anaconda\envs\torch128\python.exe' }
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable not found: $Python"
}

Push-Location $projectRoot
try {
    $extra = @()
    if ($MeasureTtft) {
        $extra += '--measure-ttft'
    }
    & $Python -m evals.p018.run_eval `
        --config evals\p018\online_config.json `
        --output-dir $OutputDir `
        --verification-timeout $VerificationTimeout `
        @extra
    if ($LASTEXITCODE -ne 0) {
        throw "P0-18 online evaluation failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
