# 开发经验沉淀

本文件只记录会影响后续工作包的环境、接口和测试陷阱；一次性的普通编译报错不在这里重复登记。

## 2026-08-20 · MSVC 必须先导入开发环境

- 现象：直接从未初始化的 PowerShell 调用固定路径 `cl.exe`，新增 C++17 目标报标准头 `cstddef` 找不到；原有构建目录因为没有重新编译，容易让人误以为工具链正常。
- 原因：MSVC 的标准库、Windows SDK 和链接器路径由 `VsDevCmd.bat` 设置，单独知道 `cl.exe` 的绝对路径不等于拥有完整编译环境。
- 最终解决：复用 `scripts/run_smoke.ps1` 的方式，在同一个 PowerShell 进程中调用 `cmd.exe /c call VsDevCmd.bat ... && set`，把返回的环境变量写回后再执行 CMake/Ninja/CTest。
- 后续避免：所有 C++ 工作包优先使用 `scripts/run_smoke.ps1` 或先执行同等 MSVC 环境导入；不要把“增量构建未触发编译”当成新代码已验证。

## 2026-08-20 · 不把偶然 Python 环境里的 Boost 当作 C++ 公共依赖

- 现象：本机 Anaconda `Library` 中能找到 Boost.JSON，但项目的固定 CMake 配置没有声明 Boost 根路径；直接依赖它会让换机器或后续 Python 环境变更时 CLI 无法构建。
- 原因：跨语言边界只需要严格 JSON 的一个小子集，使用未锁定的环境路径会把运行时依赖和 Python 环境耦合，也无法满足“避免新增不必要依赖”的边界。
- 最终解决：实现只覆盖本模块契约的严格 JSON 编解码器，拒绝重复键/未知字段/非有限数值，并将不可行矩阵的内部 INF 序列化为标准 JSON 字符串 `"INF"`。
- 后续避免：若未来要替换第三方 JSON 库，必须先补固定版本、CMake 发现方式和离线构建验证；不能直接引用 `E:\Anaconda\Library` 等个人环境路径。
