[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$OutputDir = 'tmp\p018_eval',
    [double]$VerificationTimeout = 120.0
)

# P0-18 只负责调用固定 Python 模块；数据集自身不包含可执行路径或 Shell 片段。
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
    & $Python -m evals.p018.run_eval `
        --output-dir $OutputDir `
        --verification-timeout $VerificationTimeout
    if ($LASTEXITCODE -ne 0) {
        throw "P0-18 evaluation failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
