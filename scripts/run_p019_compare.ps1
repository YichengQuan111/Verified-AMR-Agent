param(
    [string]$Python = "E:\Anaconda\envs\torch128\python.exe",
    [ValidateSet("independent", "replay", "online")]
    [string]$Mode = "independent",
    [string]$SourceReport = "tmp\p018_eval_final\p018_eval.json",
    [string]$Config = "",
    [string]$OutputDir = "tmp\p019_strategy_compare",
    [double]$VerificationTimeout = 120,
    [switch]$Resume
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# online 精确复用 P0-18 在线配置；脚本只执行门禁，不会自行启动 Smart。
if ([string]::IsNullOrWhiteSpace($Config)) {
    $Config = if ($Mode -eq "online") {
        "evals\p019\online_config.json"
    } else {
        "evals\p019\config.json"
    }
}

$Arguments = @(
    "-m", "evals.p019.run_compare",
    "--mode", $Mode,
    "--source-report", $SourceReport,
    "--config", $Config,
    "--output-dir", $OutputDir,
    "--verification-timeout", $VerificationTimeout
)
if ($Resume) {
    $Arguments += "--resume"
}
& $Python @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "P0-19 strategy comparison failed with exit code $LASTEXITCODE."
}
