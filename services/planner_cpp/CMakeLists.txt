# P0-08 把领域算法编译成独立静态库；CLI 和 CTest 只通过这个公共头调用，
# 这样 Python 后续可以复用同一实现而不复制 Hungarian 或 baseline 逻辑。
add_library(task_allocator STATIC src/task_allocator.cpp src/json_codec.cpp)
target_include_directories(task_allocator PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_features(task_allocator PUBLIC cxx_std_17)

# 保留 P0-01 工程冒烟入口，避免新算法替换后丢失原有构建门禁。
add_executable(amr_planner_smoke src/main.cpp)

# 固定 JSON stdin/stdout 的可执行边界；调用方只能通过标准输入传递快照，
# 不允许 shell=True 或隐式读取任意路径。
add_executable(task_allocator_cli src/task_allocator_main.cpp)
target_link_libraries(task_allocator_cli PRIVATE task_allocator)
target_compile_definitions(
  task_allocator_cli
  PRIVATE AMR_AGENT_VERSION="${PROJECT_VERSION}"
)

# 不引入额外测试框架；一个轻量测试程序由 CTest 拆成多个独立场景。
add_executable(task_allocator_tests tests/task_allocator_tests.cpp)
target_link_libraries(task_allocator_tests PRIVATE task_allocator)

# 同时在目标级别声明 C++17，防止该子模块被其他顶层工程引用时丢失约束。
target_compile_features(amr_planner_smoke PRIVATE cxx_std_17)
# 把顶层 project 版本编译进程序，--version 输出无需运行时读取文件。
target_compile_definitions(
  amr_planner_smoke
  PRIVATE AMR_AGENT_VERSION="${PROJECT_VERSION}"
)

set(PLANNER_CPP_TARGETS task_allocator amr_planner_smoke task_allocator_cli task_allocator_tests)
foreach(target_name IN LISTS PLANNER_CPP_TARGETS)
  if(MSVC)
    # /utf-8 让 MSVC 按 UTF-8 读取中文注释；/Zc:__cplusplus 让
    # __cplusplus 正确报告 201703，而不是旧的 199711。
    target_compile_options(
      ${target_name}
      PRIVATE /W4 /permissive- /EHsc /utf-8 /Zc:__cplusplus
    )
  else()
    target_compile_options(${target_name} PRIVATE -Wall -Wextra -Wpedantic)
  endif()
endforeach()

if(BUILD_TESTING)
  # CTest 运行 --self-test；除了退出码，还要求输出中明确包含 status=ok。
  add_test(NAME planner_cpp_smoke COMMAND amr_planner_smoke --self-test)
  set_tests_properties(
    planner_cpp_smoke
    PROPERTIES PASS_REGULAR_EXPRESSION "\"status\":\"ok\""
  )

  add_test(NAME task_allocator_normal COMMAND task_allocator_tests --case normal)
  add_test(NAME task_allocator_low_battery COMMAND task_allocator_tests --case low_battery)
  add_test(NAME task_allocator_no_feasible COMMAND task_allocator_tests --case no_feasible)
  add_test(NAME task_allocator_more_orders COMMAND task_allocator_tests --case more_orders)
  add_test(NAME task_allocator_edge_cases COMMAND task_allocator_tests --case edge_cases)
  add_test(NAME task_allocator_json_contract COMMAND task_allocator_tests --case json_contract)
endif()
