[CmdletBinding()]
param(
    [string]$Python = 'E:\Anaconda\envs\torch128\python.exe',
    [switch]$SkipCpp
)

# 任意 PowerShell 错误立即终止；外部程序的退出码则在每一步显式检查。
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$vsDevCmd = 'E:\BuildingTools\Common7\Tools\VsDevCmd.bat'
$cmake = 'E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
$ninja = 'E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe'
$ctest = 'E:\BuildingTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\ctest.exe'
$buildDir = Join-Path $projectRoot 'build\cpp'
$runtimeTemp = Join-Path $projectRoot 'tmp\smoke-runtime'
$pytestTempRoot = Join-Path $projectRoot 'tmp\pytest-runs'
$pytestBaseTemp = Join-Path $pytestTempRoot ([guid]::NewGuid().ToString('N'))
$previousTemp = $env:TEMP
$previousTmp = $env:TMP

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable not found: $Python"
}

# 受管执行环境可能不允许写 Windows 用户临时目录或既有 .pytest_cache。
# 每次使用唯一的项目内 pytest 临时目录，并禁用非验收必需的持久缓存；TMP/TEMP
# 会在 finally 中恢复，不污染调用者后续命令。
New-Item -ItemType Directory -Path $runtimeTemp -Force | Out-Null
New-Item -ItemType Directory -Path $pytestTempRoot -Force | Out-Null
$env:TEMP = $runtimeTemp
$env:TMP = $runtimeTemp

Push-Location $projectRoot
try {
    # 第 1 步：检查 Python、锁定包以及（默认情况下）C++ 工具是否匹配。
    & $Python '.\scripts\check_environment.py' $(if ($SkipCpp) { '--no-native' })
    if ($LASTEXITCODE -ne 0) { throw 'Environment check failed.' }

    # 第 2 步：执行只向前的 P0-06 迁移并确认八张核心表齐全。
    # migrate_database.py 不暴露 downgrade，因此不会在冒烟中删除核心表。
    & $Python '.\scripts\migrate_database.py' 'upgrade'
    if ($LASTEXITCODE -ne 0) { throw 'Database migration/check failed.' }

    # 第 3 步：运行全部 Python 单元、契约、真实 PostgreSQL 集成和冒烟测试。
    & $Python -m pytest -q --basetemp $pytestBaseTemp -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw 'Python smoke tests failed.' }

    if (-not $SkipCpp) {
        # 第 4 步：先验证所有固定工具路径，避免后面得到难以理解的命令错误。
        foreach ($requiredPath in @($vsDevCmd, $cmake, $ninja, $ctest)) {
            if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
                throw "C++ tool not found: $requiredPath"
            }
        }

        # VsDevCmd.bat 只能在 cmd.exe 中初始化环境。读取它生成的环境变量后，
        # 再写回当前 PowerShell 进程，后面的 CMake/Ninja 就能找到 cl.exe 和 SDK。
        $developerEnvironment = & cmd.exe /d /s /c "`"call `"$vsDevCmd`" -arch=x64 -host_arch=x64 >nul && set`""
        if ($LASTEXITCODE -ne 0) { throw 'MSVC environment initialization failed.' }
        foreach ($entry in $developerEnvironment) {
            $separator = $entry.IndexOf('=')
            if ($separator -gt 0) {
                $variableName = $entry.Substring(0, $separator)
                $variableValue = $entry.Substring($separator + 1)
                Set-Item -Path "Env:$variableName" -Value $variableValue
            }
        }

        # 第 5 步：源码与构建产物分离，所有生成文件写入 build/cpp。
        & $cmake -S $projectRoot -B $buildDir -G Ninja `
            -DCMAKE_BUILD_TYPE=Release `
            "-DCMAKE_MAKE_PROGRAM=$ninja" `
            -DBUILD_TESTING=ON
        if ($LASTEXITCODE -ne 0) { throw 'CMake configuration failed.' }

        # 第 6 步：编译 C++17 冒烟程序。
        & $cmake --build $buildDir
        if ($LASTEXITCODE -ne 0) { throw 'C++ build failed.' }

        # 第 7 步：运行 CTest；失败时显示被测程序的完整输出。
        & $ctest --test-dir $buildDir --output-on-failure
        if ($LASTEXITCODE -ne 0) { throw 'C++ smoke tests failed.' }
    }
}
finally {
    # 即使中途失败，也恢复调用脚本之前的工作目录。
    Pop-Location
    if ($null -eq $previousTemp) {
        Remove-Item Env:TEMP -ErrorAction SilentlyContinue
    }
    else {
        $env:TEMP = $previousTemp
    }
    if ($null -eq $previousTmp) {
        Remove-Item Env:TMP -ErrorAction SilentlyContinue
    }
    else {
        $env:TMP = $previousTmp
    }
}
