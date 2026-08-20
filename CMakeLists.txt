cmake_minimum_required(VERSION 3.25)

# 顶层工程只负责全局版本、语言标准和子模块编排。
project(
  AMRAgent
  VERSION 0.1.0
  DESCRIPTION "Deterministic services for the AMR Agent P0 platform"
  LANGUAGES CXX
)

# 强制使用标准 C++17，禁止编译器悄悄降级或启用厂商扩展。
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# include(CTest) 会创建 BUILD_TESTING 开关并启用 CTest。
include(CTest)

# 具体可执行程序和测试定义放在自己的服务目录中。
add_subdirectory(services/planner_cpp)
