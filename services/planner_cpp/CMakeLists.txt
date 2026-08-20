# P0-01 先建立可编译、可运行、可测试的 C++ 骨架；后续规划算法继续加在本服务中。
add_executable(amr_planner_smoke src/main.cpp)

# 同时在目标级别声明 C++17，防止该子模块被其他顶层工程引用时丢失约束。
target_compile_features(amr_planner_smoke PRIVATE cxx_std_17)
# 把顶层 project 版本编译进程序，--version 输出无需运行时读取文件。
target_compile_definitions(
  amr_planner_smoke
  PRIVATE AMR_AGENT_VERSION="${PROJECT_VERSION}"
)

if(MSVC)
  # /utf-8 让 MSVC 按 UTF-8 读取中文注释；/Zc:__cplusplus 让
  # __cplusplus 正确报告 201703，而不是旧的 199711。
  target_compile_options(
    amr_planner_smoke
    PRIVATE /W4 /permissive- /EHsc /utf-8 /Zc:__cplusplus
  )
else()
  target_compile_options(amr_planner_smoke PRIVATE -Wall -Wextra -Wpedantic)
endif()

if(BUILD_TESTING)
  # CTest 运行 --self-test；除了退出码，还要求输出中明确包含 status=ok。
  add_test(NAME planner_cpp_smoke COMMAND amr_planner_smoke --self-test)
  set_tests_properties(
    planner_cpp_smoke
    PROPERTIES PASS_REGULAR_EXPRESSION "\"status\":\"ok\""
  )
endif()
