param(
    [string]$Python = "E:\Anaconda\envs\torch128\python.exe",
    [ValidateSet("independent", "replay")]
    [string]$Mode = "independent",
    [string]$SourceReport = "tmp\p018_eval_final\p018_eval.json",
    [string]$Config = "evals\p019\config.json",
    [string]$OutputDir = "tmp\p019_strategy_compare"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# 默认独立对照三种恢复策略；replay 只做可视化，不作为发布验收。
& $Python -m evals.p019.run_compare `
    --mode $Mode `
    --source-report $SourceReport `
    --config $Config `
    --output-dir $OutputDir
if ($LASTEXITCODE -ne 0) {
    throw "P0-19 strategy comparison failed with exit code $LASTEXITCODE."
}
