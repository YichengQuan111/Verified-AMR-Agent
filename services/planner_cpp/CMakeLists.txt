# P0-08 把领域算法编译成独立静态库；CLI 和 CTest 只通过这个公共头调用，
# 这样 Python 后续可以复用同一实现而不复制 Hungarian 或 baseline 逻辑。
add_library(task_allocator STATIC src/task_allocator.cpp src/json_codec.cpp)
target_include_directories(task_allocator PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_features(task_allocator PUBLIC cxx_std_17)

# P0-09 路径库复用 P0-08 的领域结构和严格 JSON 值模型，但不复用任务分配
# 算法；A* 与 Dijkstra 的搜索实现均只在 route_planner 内部访问地图和预约表。
add_library(route_planner STATIC src/route_planner.cpp src/route_json_codec.cpp)
target_include_directories(route_planner PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(route_planner PUBLIC task_allocator)
target_compile_features(route_planner PUBLIC cxx_std_17)

# P0-10 只接收内存中的完整车队计划快照，并复用 P0-09 的 RouteStep/地图语义。
# Validator 独立成库，CLI 和 CTest 均调用同一入口，确保后续 Python 工具不能
# 复制或绕过确定性约束；错误证据由库统一排序后再序列化。
# P1-1 STL 第二判定层与规则层编进同一静态库：stl_monitor.cpp 是与车队无关的
# DSL 解析/求值核心，stl_fleet_monitor.cpp 负责从 FleetPlanRequest 独立提取
# 信号。CLI 通过显式 --stl-spec 加载规约文件，请求 JSON 不能指定规约。
add_library(fleet_plan_validator STATIC
  src/fleet_plan_validator.cpp
  src/fleet_plan_validator_json_codec.cpp
  src/stl_monitor.cpp
  src/stl_fleet_monitor.cpp
)
target_include_directories(fleet_plan_validator PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(fleet_plan_validator PUBLIC route_planner)
target_compile_features(fleet_plan_validator PUBLIC cxx_std_17)

# route_planner_cli 是 Python 后续通过固定可执行文件调用的 JSON 边界；算法
# 参数显式二选一，避免把 Dijkstra 误当成生产 A* 的隐式失败回退。
add_executable(route_planner_cli src/route_planner_main.cpp)
target_link_libraries(route_planner_cli PRIVATE route_planner)
target_compile_definitions(
  route_planner_cli
  PRIVATE AMR_AGENT_VERSION="${PROJECT_VERSION}"
)

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

# Validator CLI 只接受显式 --validate 或默认标准输入；业务 invalid 仍返回
# 结构化 evidence，进程退出码只区分契约错误和已处理的业务结果。
add_executable(fleet_plan_validator_cli src/fleet_plan_validator_main.cpp)
target_link_libraries(fleet_plan_validator_cli PRIVATE fleet_plan_validator)
target_compile_definitions(
  fleet_plan_validator_cli
  PRIVATE AMR_AGENT_VERSION="${PROJECT_VERSION}"
)

add_executable(fleet_plan_validator_tests tests/fleet_plan_validator_tests.cpp)
target_link_libraries(fleet_plan_validator_tests PRIVATE fleet_plan_validator)

# P1-1 STL 测试：DSL 语义用手工轨迹验证，车队用例复用 P0-10 的正反例做布尔
# 一致性核对；发布规约文件通过 --spec 传入，测试不隐式搜索文件系统。
add_executable(stl_monitor_tests tests/stl_monitor_tests.cpp)
target_link_libraries(stl_monitor_tests PRIVATE fleet_plan_validator)
set(STL_SPEC_FILE "${PROJECT_SOURCE_DIR}/config/stl/fleet_plan_stl_spec.json")

# 路由测试按场景拆成独立 CTest，失败时可以直接定位到障碍、预约、无解或
# 性能门禁；测试只使用公共 route_planner 头，不复制生产搜索实现。
add_executable(route_planner_tests tests/route_planner_tests.cpp)
target_link_libraries(route_planner_tests PRIVATE route_planner)

# 同时在目标级别声明 C++17，防止该子模块被其他顶层工程引用时丢失约束。
target_compile_features(amr_planner_smoke PRIVATE cxx_std_17)
# 把顶层 project 版本编译进程序，--version 输出无需运行时读取文件。
target_compile_definitions(
  amr_planner_smoke
  PRIVATE AMR_AGENT_VERSION="${PROJECT_VERSION}"
)

set(PLANNER_CPP_TARGETS
  task_allocator route_planner fleet_plan_validator route_planner_cli
  fleet_plan_validator_cli amr_planner_smoke task_allocator_cli task_allocator_tests
  fleet_plan_validator_tests route_planner_tests stl_monitor_tests
)
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

  add_test(NAME route_planner_obstacles COMMAND route_planner_tests --case obstacles)
  add_test(NAME route_planner_boundary COMMAND route_planner_tests --case boundary)
  add_test(NAME route_planner_forbidden_edges COMMAND route_planner_tests --case forbidden_edges)
  add_test(NAME route_planner_one_way_edges COMMAND route_planner_tests --case one_way_edges)
  add_test(NAME route_planner_wait COMMAND route_planner_tests --case wait)
  add_test(NAME route_planner_vertex_conflict COMMAND route_planner_tests --case vertex_conflict)
  add_test(NAME route_planner_swap_edge_conflict COMMAND route_planner_tests --case swap_edge_conflict)
  add_test(NAME route_planner_no_solution COMMAND route_planner_tests --case no_solution)
  add_test(NAME route_planner_idle_amr_reservation COMMAND route_planner_tests --case idle_amr_reservation)
  add_test(NAME route_planner_dijkstra_baseline COMMAND route_planner_tests --case dijkstra_baseline)
  add_test(NAME route_planner_reproducibility COMMAND route_planner_tests --case reproducibility)
  add_test(NAME route_planner_performance COMMAND route_planner_tests --case performance)
  add_test(NAME route_planner_json_contract COMMAND route_planner_tests --case json_contract)
  add_test(NAME route_planner_release_time_preposition COMMAND route_planner_tests --case release_time_preposition)
  add_test(NAME route_planner_release_time_early_wait COMMAND route_planner_tests --case release_time_early_wait)
  add_test(NAME route_planner_release_time_reservation_conflict COMMAND route_planner_tests --case release_time_reservation_conflict)
  add_test(NAME route_planner_release_time_infeasible_window COMMAND route_planner_tests --case release_time_infeasible_window)
  set_tests_properties(route_planner_performance PROPERTIES TIMEOUT 5)

  add_test(NAME fleet_validator_valid COMMAND fleet_plan_validator_tests --case valid)
  add_test(NAME fleet_validator_dependency COMMAND fleet_plan_validator_tests --case dependency)
  add_test(NAME fleet_validator_time_window COMMAND fleet_plan_validator_tests --case time_window)
  add_test(NAME fleet_validator_release_preposition_wait COMMAND fleet_plan_validator_tests --case release_preposition_wait)
  add_test(NAME fleet_validator_load COMMAND fleet_plan_validator_tests --case load)
  add_test(NAME fleet_validator_battery COMMAND fleet_plan_validator_tests --case battery)
  add_test(NAME fleet_validator_forbidden_zone COMMAND fleet_plan_validator_tests --case forbidden_zone)
  add_test(NAME fleet_validator_workstation_capacity COMMAND fleet_plan_validator_tests --case workstation_capacity)
  add_test(NAME fleet_validator_safety_distance COMMAND fleet_plan_validator_tests --case safety_distance)
  add_test(NAME fleet_validator_vertex_conflict COMMAND fleet_plan_validator_tests --case vertex_conflict)
  add_test(NAME fleet_validator_swap_edge_conflict COMMAND fleet_plan_validator_tests --case swap_edge_conflict)
  add_test(NAME fleet_validator_route_geometry COMMAND fleet_plan_validator_tests --case route_geometry)
  add_test(NAME fleet_validator_stable_evidence COMMAND fleet_plan_validator_tests --case stable_evidence)
  add_test(NAME fleet_validator_json_contract COMMAND fleet_plan_validator_tests --case json_contract)
  add_test(NAME fleet_validator_error_dictionary COMMAND fleet_plan_validator_tests --case error_dictionary)

  add_test(NAME stl_parse_roundtrip COMMAND stl_monitor_tests --case parse_roundtrip)
  add_test(NAME stl_atom_semantics COMMAND stl_monitor_tests --case atom_semantics)
  add_test(NAME stl_boolean_operators COMMAND stl_monitor_tests --case boolean_operators)
  add_test(NAME stl_globally_eventually COMMAND stl_monitor_tests --case globally_eventually)
  add_test(NAME stl_until_semantics COMMAND stl_monitor_tests --case until_semantics)
  add_test(NAME stl_nested_temporal COMMAND stl_monitor_tests --case nested_temporal)
  add_test(NAME stl_spec_load_rejects COMMAND stl_monitor_tests --case spec_load_rejects)
  add_test(NAME stl_spec_file COMMAND stl_monitor_tests --case spec_file --spec "${STL_SPEC_FILE}")
  add_test(NAME stl_fleet_valid_plan COMMAND stl_monitor_tests --case fleet_valid_plan --spec "${STL_SPEC_FILE}")
  add_test(NAME stl_fleet_violations COMMAND stl_monitor_tests --case fleet_violations --spec "${STL_SPEC_FILE}")
  add_test(NAME stl_boolean_consistency COMMAND stl_monitor_tests --case boolean_consistency --spec "${STL_SPEC_FILE}")
  add_test(NAME stl_gate_vs_shadow COMMAND stl_monitor_tests --case gate_vs_shadow --spec "${STL_SPEC_FILE}")
  add_test(NAME stl_stable_evidence COMMAND stl_monitor_tests --case stable_evidence --spec "${STL_SPEC_FILE}")
  add_test(NAME stl_performance COMMAND stl_monitor_tests --case performance --spec "${STL_SPEC_FILE}")
  set_tests_properties(stl_performance PROPERTIES TIMEOUT 10)
endif()
