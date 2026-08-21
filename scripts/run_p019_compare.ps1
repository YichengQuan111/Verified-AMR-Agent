param(
    [string]$Python = "E:\Anaconda\envs\torch128\python.exe",
    [string]$SourceReport = "tmp\p018_eval_final\p018_eval.json",
    [string]$Config = "evals\p019\config.json",
    [string]$OutputDir = "tmp\p019_strategy_compare"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# P0-19 只消费已经生成的 P0-18 源报告；模型服务由本入口明确不启动，Smart 也不进入路径。
& $Python -m evals.p019.run_compare `
    --source-report $SourceReport `
    --config $Config `
    --output-dir $OutputDir
if ($LASTEXITCODE -ne 0) {
    throw "P0-19 strategy comparison failed with exit code $LASTEXITCODE."
}
