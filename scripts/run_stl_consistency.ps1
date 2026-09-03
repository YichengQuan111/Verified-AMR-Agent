[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$OutputDir = ''
)

# P1-1：STL 第二判定层与规则验证器的布尔一致性核对。需要 build\cpp 已构建
# （fleet_plan_validator_cli.exe）以及 config\stl\fleet_plan_stl_spec.json。
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = if ($env:AMR_PYTHON_EXE) { $env:AMR_PYTHON_EXE } else { 'python' }
}
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $projectRoot 'tmp\stl_consistency'
}

Push-Location $projectRoot
try {
    & $Python -m evals.stl_consistency.harness --output-dir $OutputDir
    if ($LASTEXITCODE -ne 0) { throw 'STL consistency harness failed.' }
}
finally {
    Pop-Location
}
