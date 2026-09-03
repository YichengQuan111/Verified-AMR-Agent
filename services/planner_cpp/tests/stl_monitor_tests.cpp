#include "fleet_plan_validator/fleet_plan_validator.hpp"
#include "fleet_plan_validator/json_codec.hpp"
#include "fleet_plan_validator/stl_monitor.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <iostream>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

// P1-1 STL 监控器 CTest：先验证 DSL 解析与离散时间语义（与车队无关），再用
// P0-10 CTest 的同一批正反例验证“公式违反 ⟺ 规则层对应错误码出现”的布尔
// 一致性，以及 gate/shadow、稳定序列化和性能门禁。
namespace {

using amr::planner::AMRState;
using amr::planner::AMRTaskStatus;
using amr::planner::ConnectionStatus;
using amr::planner::GridPosition;
using amr::planner::HealthStatus;
using amr::planner::Location;
using amr::planner::RouteAction;
using amr::planner::RouteEdge;
using amr::planner::RouteStep;
using amr::planner::TransportOrder;
namespace validator = amr::planner::validator;
namespace stl = amr::planner::stl;
using validator::FleetPlanRequest;
using validator::FleetPlanRoute;

class TestFailure final : public std::runtime_error {
 public:
  explicit TestFailure(const std::string& message) : std::runtime_error(message) {}
};

void expect(bool condition, const std::string& message) {
  if (!condition) throw TestFailure(message);
}

void expect_near(double observed, double expected, const std::string& message) {
  if (std::fabs(observed - expected) > 1.0e-9) {
    throw TestFailure(message + " (observed=" + std::to_string(observed) +
                      ", expected=" + std::to_string(expected) + ")");
  }
}

std::string g_spec_path;

const std::string& spec_path() {
  if (g_spec_path.empty()) throw TestFailure("本用例需要 --spec <规约文件路径>");
  return g_spec_path;
}

stl::Specification load_spec_file() {
  std::ifstream input(spec_path(), std::ios::binary);
  expect(static_cast<bool>(input), "无法打开规约文件: " + spec_path());
  std::stringstream buffer;
  buffer << input.rdbuf();
  return stl::parse_specification_text(buffer.str());
}

// ---------------------------------------------------------------------------
// 通用轨迹构造
// ---------------------------------------------------------------------------

stl::SignalTrace trace_of(std::initializer_list<std::pair<const char*, std::vector<double>>> signals,
                          int start_time = 0) {
  stl::SignalTrace trace;
  trace.start_time = start_time;
  for (const auto& entry : signals) {
    trace.length = entry.second.size();
    trace.signals.emplace(entry.first, entry.second);
  }
  return trace;
}

stl::Evaluation eval(const std::string& text, const stl::SignalTrace& trace) {
  return stl::evaluate(stl::parse_formula(text), trace);
}

// ---------------------------------------------------------------------------
// 车队 fixture（与 fleet_plan_validator_tests.cpp 相同的正反例）
// ---------------------------------------------------------------------------

AMRState make_amr(const std::string& id, GridPosition position, int heading = 90,
                 double battery = 100.0) {
  return AMRState{id, position, heading, battery, 0.0, AMRTaskStatus::kIdle,
                  HealthStatus::kHealthy, ConnectionStatus::kOnline};
}

TransportOrder make_order(const std::string& id, const std::string& pickup,
                          const std::string& dropoff) {
  return TransportOrder{id, "material-" + id, pickup, dropoff, 3, 0, 20, {}};
}

RouteStep step(GridPosition position, int heading, int time, RouteAction action, double g_cost) {
  return RouteStep{position, heading, time, action, g_cost};
}

FleetPlanRoute make_route(const std::string& amr_id, const std::string& order_id, double payload,
                          int pickup_time, int dropoff_time, std::vector<RouteStep> path) {
  FleetPlanRoute route;
  route.amr_id = amr_id;
  route.order_id = order_id;
  route.payload_kg = payload;
  route.pickup_time = pickup_time;
  route.dropoff_time = dropoff_time;
  route.path = std::move(path);
  return route;
}

FleetPlanRequest base_request() {
  FleetPlanRequest request;
  request.environment_ref = "warehouse-test";
  request.map.width = 10;
  request.map.height = 5;
  request.start_time = 0;
  request.max_time = 20;
  request.config.maximum_load_kg = 100.0;
  request.config.energy_per_cell_percent = 1.0;
  request.config.battery_safety_reserve_percent = 15.0;
  request.config.new_task_battery_threshold_percent = 20.0;
  request.config.critical_battery_threshold_percent = 10.0;
  request.config.minimum_safety_distance_cells = 1;
  request.config.default_workstation_capacity = 1;
  request.amrs = {make_amr("A1", GridPosition{0, 0}), make_amr("A2", GridPosition{0, 4})};
  request.orders = {make_order("O1", "P1", "D1"), make_order("O2", "P2", "D2")};
  request.locations = {
      Location{"P1", GridPosition{1, 0}},
      Location{"D1", GridPosition{2, 0}},
      Location{"P2", GridPosition{1, 4}},
      Location{"D2", GridPosition{2, 4}},
  };
  request.workstation_capacities = {{"P1", 1}, {"D1", 1}, {"P2", 1}, {"D2", 1}};
  request.routes = {
      make_route("A1", "O1", 5.0, 1, 2,
                 {step({0, 0}, 90, 0, RouteAction::kStart, 0.0),
                  step({1, 0}, 90, 1, RouteAction::kMove, 1.0),
                  step({2, 0}, 90, 2, RouteAction::kMove, 2.0)}),
      make_route("A2", "O2", 5.0, 1, 2,
                 {step({0, 4}, 90, 0, RouteAction::kStart, 0.0),
                  step({1, 4}, 90, 1, RouteAction::kMove, 1.0),
                  step({2, 4}, 90, 2, RouteAction::kMove, 2.0)}),
  };
  return request;
}

FleetPlanRequest workstation_capacity_request() {
  auto request = base_request();
  request.amrs = {make_amr("A1", GridPosition{0, 1}, 90), make_amr("A2", GridPosition{2, 1}, 270)};
  request.orders[0] = make_order("O1", "P1", "D1");
  request.orders[1] = make_order("O2", "P1", "D2");
  request.locations = {
      Location{"P1", GridPosition{1, 1}},
      Location{"D1", GridPosition{0, 0}},
      Location{"D2", GridPosition{2, 2}},
  };
  request.workstation_capacities = {{"P1", 1}, {"D1", 1}, {"D2", 1}};
  request.routes = {
      make_route("A1", "O1", 5.0, 1, 5,
                 {step({0, 1}, 90, 0, RouteAction::kStart, 0.0),
                  step({1, 1}, 90, 1, RouteAction::kMove, 1.0),
                  step({1, 1}, 0, 2, RouteAction::kTurnLeft, 1.25),
                  step({1, 0}, 0, 3, RouteAction::kMove, 2.25),
                  step({0, 0}, 270, 4, RouteAction::kTurnLeft, 2.5),
                  step({0, 0}, 270, 5, RouteAction::kMove, 3.5)}),
      make_route("A2", "O2", 5.0, 1, 5,
                 {step({2, 1}, 270, 0, RouteAction::kStart, 0.0),
                  step({1, 1}, 270, 1, RouteAction::kMove, 1.0),
                  step({1, 1}, 180, 2, RouteAction::kTurnLeft, 1.25),
                  step({1, 2}, 180, 3, RouteAction::kMove, 2.25),
                  step({1, 2}, 90, 4, RouteAction::kTurnLeft, 2.5),
                  step({2, 2}, 90, 5, RouteAction::kMove, 3.5)}),
  };
  return request;
}

FleetPlanRequest safety_distance_request() {
  auto request = base_request();
  request.config.minimum_safety_distance_cells = 2;
  request.amrs = {make_amr("A1", GridPosition{0, 0}, 90), make_amr("A2", GridPosition{1, 0}, 90)};
  request.locations = {
      Location{"P1", GridPosition{0, 0}}, Location{"D1", GridPosition{0, 1}},
      Location{"P2", GridPosition{1, 0}}, Location{"D2", GridPosition{1, 1}},
  };
  request.routes = {
      make_route("A1", "O1", 1.0, 0, 2,
                 {step({0, 0}, 90, 0, RouteAction::kStart, 0.0),
                  step({0, 0}, 180, 1, RouteAction::kTurnRight, 0.25),
                  step({0, 1}, 180, 2, RouteAction::kMove, 1.25)}),
      make_route("A2", "O2", 1.0, 0, 2,
                 {step({1, 0}, 90, 0, RouteAction::kStart, 0.0),
                  step({1, 0}, 180, 1, RouteAction::kTurnRight, 0.25),
                  step({1, 1}, 180, 2, RouteAction::kMove, 1.25)}),
  };
  return request;
}

FleetPlanRequest vertex_conflict_request() {
  auto request = base_request();
  request.amrs = {make_amr("A1", GridPosition{0, 0}, 90), make_amr("A2", GridPosition{2, 0}, 270)};
  request.locations = {
      Location{"P1", GridPosition{1, 0}}, Location{"D1", GridPosition{2, 0}},
      Location{"P2", GridPosition{1, 0}}, Location{"D2", GridPosition{2, 1}},
  };
  request.workstation_capacities = {{"P1", 2}, {"D1", 1}, {"P2", 2}, {"D2", 1}};
  request.routes = {
      make_route("A1", "O1", 1.0, 1, 2,
                 {step({0, 0}, 90, 0, RouteAction::kStart, 0.0),
                  step({1, 0}, 90, 1, RouteAction::kMove, 1.0),
                  step({2, 0}, 90, 2, RouteAction::kMove, 2.0)}),
      make_route("A2", "O2", 1.0, 1, 5,
                 {step({2, 0}, 270, 0, RouteAction::kStart, 0.0),
                  step({1, 0}, 270, 1, RouteAction::kMove, 1.0),
                  step({1, 0}, 180, 2, RouteAction::kTurnLeft, 1.25),
                  step({1, 1}, 180, 3, RouteAction::kMove, 2.25),
                  step({1, 1}, 90, 4, RouteAction::kTurnLeft, 2.5),
                  step({2, 1}, 90, 5, RouteAction::kMove, 3.5)}),
  };
  return request;
}

FleetPlanRequest swap_edge_request() {
  auto request = base_request();
  request.amrs = {make_amr("A1", GridPosition{0, 1}, 90), make_amr("A2", GridPosition{1, 1}, 270)};
  request.locations = {
      Location{"P1", GridPosition{1, 1}}, Location{"D1", GridPosition{1, 2}},
      Location{"P2", GridPosition{0, 1}}, Location{"D2", GridPosition{0, 2}},
  };
  request.routes = {
      make_route("A1", "O1", 1.0, 1, 3,
                 {step({0, 1}, 90, 0, RouteAction::kStart, 0.0),
                  step({1, 1}, 90, 1, RouteAction::kMove, 1.0),
                  step({1, 1}, 180, 2, RouteAction::kTurnRight, 1.25),
                  step({1, 2}, 180, 3, RouteAction::kMove, 2.25)}),
      make_route("A2", "O2", 1.0, 1, 3,
                 {step({1, 1}, 270, 0, RouteAction::kStart, 0.0),
                  step({0, 1}, 270, 1, RouteAction::kMove, 1.0),
                  step({0, 1}, 180, 2, RouteAction::kTurnLeft, 1.25),
                  step({0, 2}, 180, 3, RouteAction::kMove, 2.25)}),
  };
  return request;
}

const stl::InstanceResult& find_result(const stl::MonitorReport& report,
                                       const std::string& formula_id,
                                       const std::string& primary_id) {
  for (const auto& result : report.results) {
    if (result.formula_id != formula_id) continue;
    if (primary_id.empty() || result.scope.order_id == primary_id ||
        result.scope.amr_id == primary_id || result.scope.station_id == primary_id) {
      return result;
    }
  }
  throw TestFailure("缺少 STL 实例结果: " + formula_id + "/" + primary_id);
}

bool formula_violated(const stl::MonitorReport& report, const std::string& formula_id) {
  return std::any_of(report.results.begin(), report.results.end(), [&](const auto& result) {
    return result.formula_id == formula_id && !result.satisfied;
  });
}

bool has_code(const validator::ValidationResult& result, const std::string& code) {
  return std::any_of(result.errors.begin(), result.errors.end(),
                     [&](const auto& error) { return error.code == code; });
}

// ---------------------------------------------------------------------------
// 用例
// ---------------------------------------------------------------------------

void test_parse_roundtrip() {
  const std::vector<std::string> formulas = {
      "G(battery >= 15)",
      "G[0, 0](battery > new_task_battery_threshold_percent) and G(battery >= 15)",
      "F[release_time, deadline](delivered_margin >= 0)",
      "(a <= -1) U[2, inf] (b >= 0)",
      "G(p >= 1 or F[0, 30](q > 0))",
      "not (x < 3) -> F(y >= 2)",
      "a >= 1 and b >= 2 or c >= 3",
  };
  for (const auto& text : formulas) {
    const auto parsed = stl::parse_formula(text);
    const std::string canonical = stl::format_formula(parsed);
    const auto reparsed = stl::parse_formula(canonical);
    expect(stl::format_formula(reparsed) == canonical, "规范文本必须可往返: " + text);
  }
  // 优先级：and 高于 or，蕴含最低，且右结合。
  expect(stl::format_formula(stl::parse_formula("a >= 1 and b >= 2 or c >= 3")) ==
             "((a >= 1) and (b >= 2)) or (c >= 3)",
         "and 的优先级必须高于 or");
  expect(stl::format_formula(stl::parse_formula("a >= 1 -> b >= 2 -> c >= 3")) ==
             "(a >= 1) -> ((b >= 2) -> (c >= 3))",
         "蕴含必须右结合");
  for (const std::string bad : {"G[1, 0](a >= 1)", "G[inf, 2](a >= 1)", "a >= ", "a == 1",
                                "(a >= 1", "G[-1, 2](a >= 1)", "F[0, 2.5](a >= 1)", "and >= 1",
                                "a >= 1 b >= 2", "a >= 1 $"}) {
    bool rejected = false;
    try {
      (void)stl::parse_formula(bad);
    } catch (const stl::SpecificationError&) {
      rejected = true;
    }
    expect(rejected, "非法公式必须被拒绝: " + bad);
  }
}

void test_atom_semantics() {
  const auto trace = trace_of({{"x", {5.0}}});
  expect_near(eval("x >= 5", trace).robustness, 0.0, ">= 边界鲁棒度为 0");
  expect(eval("x >= 5", trace).satisfied, ">= 在边界满足");
  expect(!eval("x > 5", trace).satisfied, "> 在边界不满足");
  expect_near(eval("x > 5", trace).robustness, 0.0, "> 边界鲁棒度仍为 0");
  expect(eval("x <= 5", trace).satisfied, "<= 在边界满足");
  expect(!eval("x < 5", trace).satisfied, "< 在边界不满足");
  expect_near(eval("x <= 8", trace).robustness, 3.0, "<= 的鲁棒度是上界减信号");
  expect_near(eval("x >= -1", trace).robustness, 6.0, "负数字面阈值必须可解析");
  auto with_parameter = trace;
  with_parameter.parameters["limit"] = 2.0;
  expect_near(eval("x >= limit", with_parameter).robustness, 3.0, "阈值参数必须从轨迹参数解析");
  bool rejected = false;
  try {
    (void)eval("x >= missing", trace);
  } catch (const stl::SpecificationError&) {
    rejected = true;
  }
  expect(rejected, "缺失参数必须失败而不是取 0");
  const auto shifted = trace_of({{"x", {5.0, 4.0}}}, 7);
  expect(eval("G(x >= 5)", shifted).weakest_time.value() == 8, "weakest_time 必须换算成绝对时间");
}

void test_boolean_operators() {
  const auto trace = trace_of({{"a", {3.0}}, {"b", {-2.0}}});
  const auto conjunction = eval("a >= 0 and b >= 0", trace);
  expect(!conjunction.satisfied && conjunction.robustness == -2.0, "and 取最小鲁棒度");
  const auto disjunction = eval("a >= 0 or b >= 0", trace);
  expect(disjunction.satisfied && disjunction.robustness == 3.0, "or 取最大鲁棒度");
  const auto negation = eval("not (b >= 0)", trace);
  expect(negation.satisfied && negation.robustness == 2.0, "not 取反鲁棒度");
  const auto implication = eval("a >= 0 -> b >= 0", trace);
  expect(!implication.satisfied && implication.robustness == -2.0, "前件成立时蕴含等于后件");
  const auto vacuous_implication = eval("b >= 0 -> a >= 100", trace);
  expect(vacuous_implication.satisfied && vacuous_implication.robustness == 2.0,
         "前件不成立时蕴含由前件的取反决定");
  const auto truth = eval("true", trace);
  expect(truth.satisfied && truth.vacuous, "true 的鲁棒度为 +inf 并标记 vacuous");
}

void test_globally_eventually() {
  const auto trace = trace_of({{"x", {5.0, 3.0, 1.0, 4.0, 6.0}}});
  const auto always = eval("G(x >= 0)", trace);
  expect(always.satisfied && always.robustness == 1.0 && always.weakest_time.value() == 2,
         "G 取全程最小值及其时刻");
  const auto window = eval("G[0, 1](x >= 0)", trace);
  expect(window.robustness == 3.0 && window.weakest_time.value() == 1, "G 区间只看窗口内");
  const auto violated = eval("G(x >= 2)", trace);
  expect(!violated.satisfied && violated.robustness == -1.0 && violated.weakest_time.value() == 2,
         "G 违反时给出最薄弱时刻");
  const auto empty_window = eval("G[10, 20](x >= 100)", trace);
  expect(empty_window.satisfied && empty_window.vacuous && !empty_window.weakest_time.has_value(),
         "G 在轨迹之外的窗口空真");
  const auto eventually = eval("F(x >= 6)", trace);
  expect(eventually.satisfied && eventually.robustness == 0.0 && eventually.weakest_time.value() == 4,
         "F 取全程最大值及其时刻");
  const auto eventually_empty = eval("F[10, 20](x >= 0)", trace);
  expect(!eventually_empty.satisfied && eventually_empty.vacuous, "F 在空窗口为假");
  const auto clipped = eval("F[3, 100](x >= 0)", trace);
  expect(clipped.robustness == 6.0, "F 窗口超出轨迹时截断到轨迹末尾");
  // 违反时 weakest_time 必须指向真正违反的时刻，而不是鲁棒度恰好为 0 的满足点。
  const auto strict_trace = trace_of({{"x", {0.0, 0.0}}, {"y", {1.0, 0.0}}});
  const auto strict = eval("G(x >= 0 and y > 0)", strict_trace);
  expect(!strict.satisfied && strict.weakest_time.value() == 1,
         "违反的 weakest_time 必须优先选择未满足的时刻");
}

void test_until_semantics() {
  // 依赖时序：当前订单 t=1 装货，前置订单 t=2 交付 → 违反，鲁棒度 -1。
  auto trace = trace_of({{"loaded", {-1.0, 0.0, 1.0, 2.0, 3.0}}, {"delivered", {-2.0, -1.0, 0.0, 1.0, 2.0}}});
  const auto violated = eval("(loaded <= -1) U (delivered >= 0)", trace);
  expect(!violated.satisfied && violated.robustness == -1.0 && violated.weakest_time.value() == 1,
         "装货早于前置交付时 until 违反");
  // 装货推迟到 t=4：裕量 2，鲁棒度为其一半。
  trace = trace_of({{"loaded", {-4.0, -3.0, -2.0, -1.0, 0.0, 1.0}},
                    {"delivered", {-2.0, -1.0, 0.0, 1.0, 2.0, 3.0}}});
  const auto satisfied = eval("(loaded <= -1) U (delivered >= 0)", trace);
  expect(satisfied.satisfied && satisfied.robustness == 1.0, "until 鲁棒度约为时序裕量一半");
  const auto bounded = eval("(loaded <= -1) U[0, 1] (delivered >= 0)", trace);
  expect(!bounded.satisfied, "区间内没有交付时 until 违反");
  const auto empty = eval("(loaded <= -1) U[10, 20] (delivered >= 0)", trace);
  expect(!empty.satisfied && empty.vacuous, "until 空窗口为假");
  // 前置从未交付（裕量始终为负）：until 必然违反，不会被误判为空真。
  trace = trace_of({{"loaded", {-4.0, -3.0, -2.0}}, {"delivered", {-9.0, -8.0, -7.0}}});
  expect(!eval("(loaded <= -1) U (delivered >= 0)", trace).satisfied, "从未交付必须违反");
}

void test_nested_temporal() {
  // G(p -> F[0,2] q)：p 在 t=1 触发，q 在 t=3 响应（2 步内），t=4 触发后无响应。
  const auto trace = trace_of({{"p", {-1.0, 1.0, -1.0, -1.0, 1.0, -1.0}},
                               {"q", {-1.0, -1.0, -1.0, 1.0, -1.0, -1.0}}});
  const auto responded = eval("G[0, 3](p > 0 -> F[0, 2](q > 0))", trace);
  expect(responded.satisfied, "触发后两步内响应必须满足");
  const auto unanswered = eval("G(p > 0 -> F[0, 2](q > 0))", trace);
  expect(!unanswered.satisfied && unanswered.weakest_time.value() == 4,
         "无响应的触发必须定位到触发时刻");
}

void test_spec_load_rejects() {
  const std::string valid = R"json({
    "schema_version": "1.0", "spec_id": "s", "spec_version": "v", "enforcement": "gate",
    "formulas": [{"id": "f", "scope": "amr", "description": "d", "formula": "G(battery >= 0)",
                  "rule_codes": ["x"], "warn_below": 1}]
  })json";
  const auto spec = stl::parse_specification_text(valid);
  expect(spec.formulas.size() == 1 && spec.formulas[0].warn_below.value() == 1.0,
         "合法规约必须可加载");
  const std::vector<std::pair<std::string, std::string>> bad_specs = {
      {"未知顶层字段", R"json({"schema_version":"1.0","spec_id":"s","spec_version":"v","enforcement":"gate","llm_override":true,"formulas":[{"id":"f","scope":"amr","description":"d","formula":"G(battery >= 0)","rule_codes":[]}]})json"},
      {"作用域未知信号", R"json({"schema_version":"1.0","spec_id":"s","spec_version":"v","enforcement":"gate","formulas":[{"id":"f","scope":"order","description":"d","formula":"G(battery >= 0)","rule_codes":[]}]})json"},
      {"作用域未知参数", R"json({"schema_version":"1.0","spec_id":"s","spec_version":"v","enforcement":"gate","formulas":[{"id":"f","scope":"amr","description":"d","formula":"G(battery >= capacity)","rule_codes":[]}]})json"},
      {"重复公式 id", R"json({"schema_version":"1.0","spec_id":"s","spec_version":"v","enforcement":"gate","formulas":[{"id":"f","scope":"amr","description":"d","formula":"G(battery >= 0)","rule_codes":[]},{"id":"f","scope":"amr","description":"d","formula":"G(load >= 0)","rule_codes":[]}]})json"},
      {"非法 enforcement", R"json({"schema_version":"1.0","spec_id":"s","spec_version":"v","enforcement":"advisory","formulas":[{"id":"f","scope":"amr","description":"d","formula":"G(battery >= 0)","rule_codes":[]}]})json"},
      {"空公式列表", R"json({"schema_version":"1.0","spec_id":"s","spec_version":"v","enforcement":"gate","formulas":[]})json"},
      {"错误 schema_version", R"json({"schema_version":"2.0","spec_id":"s","spec_version":"v","enforcement":"gate","formulas":[{"id":"f","scope":"amr","description":"d","formula":"G(battery >= 0)","rule_codes":[]}]})json"},
      {"公式语法错误", R"json({"schema_version":"1.0","spec_id":"s","spec_version":"v","enforcement":"gate","formulas":[{"id":"f","scope":"amr","description":"d","formula":"G(battery >=)","rule_codes":[]}]})json"},
      {"JSON 非法", "{not json"},
  };
  for (const auto& entry : bad_specs) {
    bool rejected = false;
    try {
      (void)stl::parse_specification_text(entry.second);
    } catch (const stl::SpecificationError&) {
      rejected = true;
    }
    expect(rejected, "规约必须拒绝: " + entry.first);
  }
}

void test_spec_file() {
  const auto spec = load_spec_file();
  expect(spec.spec_id == "amr-fleet-plan-stl" && spec.spec_version == "p1-1.v1",
         "规约文件身份必须固定");
  expect(spec.enforcement == stl::Enforcement::kGate, "发布规约必须是 gate 模式");
  expect(spec.formulas.size() == 8, "发布规约必须恰好包含 8 条公式");
  std::set<std::string> ids;
  std::set<std::string> covered;
  for (const auto& formula : spec.formulas) {
    expect(ids.insert(formula.id).second, "公式 id 必须唯一");
    for (const auto& code : formula.rule_codes) {
      expect(validator::error_dictionary().end() !=
                 std::find_if(validator::error_dictionary().begin(),
                              validator::error_dictionary().end(),
                              [&](const auto& definition) { return definition.code == code; }),
             "rule_codes 必须是错误字典中的稳定错误码: " + code);
      covered.insert(code);
    }
  }
  for (const std::string required : {"time_window", "battery_safety", "traffic_rules",
                                     "load_capacity", "fleet_separation", "workstation_capacity",
                                     "dependency_precedence", "low_battery_charging"}) {
    expect(ids.count(required) == 1U, "发布规约缺少公式: " + required);
  }
  for (const std::string code : {"pickup_before_release", "dropoff_after_deadline",
                                 "battery_safety_reserve_breached", "forbidden_zone_occupied",
                                 "load_capacity_exceeded", "vertex_conflict", "swap_edge_conflict",
                                 "safety_distance_breached", "workstation_capacity_exceeded",
                                 "task_dependency_time_order"}) {
    expect(covered.count(code) == 1U, "发布规约必须覆盖安全约束错误码: " + code);
  }
  const auto described = amr::planner::json::serialize(stl::specification_to_value(spec));
  expect(described.find("\"formula_count\":8") != std::string::npos &&
             described.find("normalized_formula") != std::string::npos,
         "规约描述 JSON 必须包含公式数量与规范文本");
}

void test_fleet_valid_plan() {
  const auto spec = load_spec_file();
  const auto request = base_request();
  const auto report = validator::monitor_fleet_plan(request, spec);
  expect(report.status == "satisfied" && report.satisfied && report.violated_count == 0,
         "合法计划必须满足全部 STL 规约");
  expect(report.instance_count == 15, "基础计划应实例化 15 条公式实例");
  expect_near(find_result(report, "time_window", "O1").robustness.value(), 18.0,
              "时间窗鲁棒度 = deadline - dropoff_time");
  expect(find_result(report, "time_window", "O1").weakest_time.value() == 20,
         "时间窗最薄弱时刻是决定裕量的 deadline");
  expect_near(find_result(report, "battery_safety", "A1").robustness.value(), 80.0,
              "电量鲁棒度 = min(派发裕量 80, 余量裕量 83)");
  expect_near(find_result(report, "load_capacity", "A1").robustness.value(), 95.0,
              "载荷鲁棒度 = 上限 - 装货后载荷");
  expect(find_result(report, "load_capacity", "A1").weakest_time.value() == 1, "载荷最薄弱时刻是装货时刻");
  expect_near(find_result(report, "fleet_separation", "A1").robustness.value(), 1.0,
              "含布尔子式的车队间距鲁棒度饱和在 1");
  expect_near(find_result(report, "workstation_capacity", "P1").robustness.value(), 0.0,
              "满容量服务的工位鲁棒度为 0 且不算险胜");
  expect_near(find_result(report, "low_battery_charging", "A1").robustness.value(), 88.0,
              "临界电量规约鲁棒度 = 末端电量 - 临界门槛");
  expect(report.narrow_pass_count == 0, "基础计划没有险胜");
  expect(report.min_robustness.value() == 0.0 && report.min_robustness_formula_id == "traffic_rules",
         "最小鲁棒度应来自贴边行驶的 traffic_rules");

  const auto gated = validator::validate_fleet_plan(request, &spec);
  expect(gated.valid && gated.errors.empty() && gated.stl.has_value() && gated.stl->satisfied,
         "gate 模式下合法计划仍然 valid 且附带 STL 报告");
  expect(!validator::validate_fleet_plan(request).stl.has_value(), "不传规约时结果不含 STL 报告");

  auto narrow = request;
  narrow.orders[0].deadline = 4;
  const auto narrow_report = validator::monitor_fleet_plan(narrow, spec);
  expect(narrow_report.satisfied && narrow_report.narrow_pass_count == 1 &&
             find_result(narrow_report, "time_window", "O1").narrow_pass,
         "时间窗裕量低于 warn_below 时记为险胜但仍满足");
}

struct Scenario {
  std::string name;
  FleetPlanRequest request;
  std::string formula_id;
  double expected_robustness;
  int expected_weakest_time;
};

std::vector<Scenario> violation_scenarios() {
  std::vector<Scenario> scenarios;
  {
    auto request = base_request();
    request.orders[1].dependencies = {"O1"};
    scenarios.push_back({"dependency", request, "dependency_precedence", -1.0, 1});
  }
  {
    auto request = base_request();
    request.orders[0].deadline = 1;
    scenarios.push_back({"deadline", request, "time_window", -1.0, 1});
  }
  {
    auto request = base_request();
    request.orders[0].release_time = 2;
    scenarios.push_back({"release", request, "time_window", -1.0, 1});
  }
  {
    auto request = base_request();
    request.routes[0].payload_kg = 101.0;
    scenarios.push_back({"load", request, "load_capacity", -1.0, 1});
  }
  {
    auto request = base_request();
    request.amrs[0].battery = 16.0;
    scenarios.push_back({"battery", request, "battery_safety", -4.0, 0});
  }
  {
    auto request = base_request();
    request.map.blocked_cells = {GridPosition{1, 0}};
    scenarios.push_back({"forbidden_zone", request, "traffic_rules", -1.0, 1});
  }
  {
    auto request = base_request();
    request.map.blocked_edges = {RouteEdge{GridPosition{0, 0}, GridPosition{1, 0}}};
    scenarios.push_back({"forbidden_edge", request, "traffic_rules", -1.0, 1});
  }
  {
    auto request = base_request();
    request.map.one_way_edges = {RouteEdge{GridPosition{1, 0}, GridPosition{0, 0}}};
    scenarios.push_back({"one_way", request, "traffic_rules", -1.0, 1});
  }
  scenarios.push_back({"workstation_capacity", workstation_capacity_request(),
                       "workstation_capacity", -1.0, 1});
  scenarios.push_back({"safety_distance", safety_distance_request(), "fleet_separation", -1.0, 0});
  scenarios.push_back({"vertex_conflict", vertex_conflict_request(), "fleet_separation", -1.0, 1});
  scenarios.push_back({"swap_edge", swap_edge_request(), "fleet_separation", -1.0, 0});
  {
    auto request = base_request();
    request.routes[0].path.pop_back();  // 终点不再是 dropoff
    request.routes[0].dropoff_time = 1;
    scenarios.push_back({"dropoff_not_reached", request, "time_window", -1.0, 20});
  }
  return scenarios;
}

void test_fleet_violations() {
  const auto spec = load_spec_file();
  for (const auto& scenario : violation_scenarios()) {
    const auto report = validator::monitor_fleet_plan(scenario.request, spec);
    expect(!report.satisfied && report.status == "violated",
           scenario.name + ": 反例必须被 STL 判为违反");
    bool found = false;
    for (const auto& result : report.results) {
      if (result.formula_id != scenario.formula_id || result.satisfied) continue;
      found = true;
      expect_near(result.robustness.value(), scenario.expected_robustness,
                  scenario.name + ": 违反鲁棒度必须精确");
      expect(result.weakest_time.value() == scenario.expected_weakest_time,
             scenario.name + ": 最薄弱时刻必须精确 (observed=" +
                 std::to_string(result.weakest_time.value()) + ")");
      expect(result.coordinate.has_value() || result.scope.kind == stl::ScopeKind::kStation,
             scenario.name + ": 违反证据必须附带坐标");
      break;
    }
    expect(found, scenario.name + ": 期望公式 " + scenario.formula_id + " 被违反");
  }
}

void test_boolean_consistency() {
  // 对全部正反例做“公式违反 ⟺ 至少一个映射错误码出现”的双向核对；这是 60 例
  // 一致性 harness 的 C++ 缩影，任何不一致都意味着两层之一有 Bug。
  const auto spec = load_spec_file();
  std::vector<FleetPlanRequest> requests = {base_request()};
  for (const auto& scenario : violation_scenarios()) requests.push_back(scenario.request);
  {
    auto request = base_request();
    request.orders[0].deadline = 4;
    requests.push_back(request);
  }
  std::size_t checks = 0;
  for (const auto& request : requests) {
    const auto rules = validator::validate_fleet_plan(request);
    const auto report = validator::monitor_fleet_plan(request, spec);
    for (const auto& formula : spec.formulas) {
      if (formula.rule_codes.empty()) continue;
      const bool violated = formula_violated(report, formula.id);
      const bool expected = std::any_of(formula.rule_codes.begin(), formula.rule_codes.end(),
                                        [&](const auto& code) { return has_code(rules, code); });
      expect(violated == expected, "布尔不一致: 公式 " + formula.id + " violated=" +
                                       std::to_string(violated) + " rule=" + std::to_string(expected));
      ++checks;
    }
  }
  expect(checks >= 100, "一致性核对次数必须覆盖全部公式×场景");
}

void test_gate_vs_shadow() {
  // 规则层不检查“全程电量 ≥ 99”，因此只有 STL 会违反；gate 必须拒绝，shadow 只记录。
  const std::string text = R"json({
    "schema_version": "1.0", "spec_id": "strict", "spec_version": "test", "enforcement": "gate",
    "formulas": [{"id": "strict_battery", "scope": "amr", "description": "d",
                  "formula": "G(battery >= 99)", "rule_codes": [], "warn_below": null}]
  })json";
  auto gate = stl::parse_specification_text(text);
  const auto request = base_request();
  const auto gated = validator::validate_fleet_plan(request, &gate);
  expect(!gated.valid && gated.status == "invalid" && has_code(gated, "stl_specification_violated"),
         "gate 模式下 STL 违反必须使计划 invalid");
  const auto& error = *std::find_if(gated.errors.begin(), gated.errors.end(), [](const auto& item) {
    return item.code == "stl_specification_violated";
  });
  expect(error.constraint == "stl_specification" && error.amr_id == "A1" &&
             error.time.value() == 2 && error.observed.value() == -1.0 && error.limit.value() == 0.0 &&
             error.coordinate.has_value() && error.message.find("strict_battery") != std::string::npos,
         "STL 错误证据必须带公式 id、AMR、时刻、鲁棒度与坐标");
  expect(gated.stl.has_value() && gated.stl->status == "violated" && gated.stl->violated_count == 2,
         "gate 模式下报告仍完整");

  auto shadow = gate;
  shadow.enforcement = stl::Enforcement::kShadow;
  const auto shadowed = validator::validate_fleet_plan(request, &shadow);
  expect(shadowed.valid && shadowed.errors.empty() && shadowed.stl.has_value() &&
             shadowed.stl->status == "violated",
         "shadow 模式只记录，不改变规则层结论");

  // 规则层已经拒绝的结构错误（地图非法）下 STL 跳过而不是崩溃。
  auto broken = request;
  broken.map.width = 0;
  const auto skipped = validator::validate_fleet_plan(broken, &gate);
  expect(!skipped.valid && skipped.stl.has_value() && skipped.stl->status == "skipped" &&
             !skipped.stl->skip_reason.empty(),
         "无法构造轨迹时 STL 报告为 skipped");
}

void test_stable_evidence() {
  const auto spec = load_spec_file();
  auto request = base_request();
  request.amrs[0].battery = 16.0;
  const auto first = amr::planner::json::serialize(
      amr::planner::validator_json::result_to_value(validator::validate_fleet_plan(request, &spec)));
  const auto second = amr::planner::json::serialize(
      amr::planner::validator_json::result_to_value(validator::validate_fleet_plan(request, &spec)));
  expect(first == second, "同一计划的 STL 证据必须字节一致");
  expect(first.find("\"stl\":{") != std::string::npos &&
             first.find("\"formula_id\":\"battery_safety\"") != std::string::npos &&
             first.find("\"narrow_pass\"") != std::string::npos &&
             first.find("\"weakest_time\":0") != std::string::npos,
         "序列化必须包含 STL 报告字段");
  const auto plain = amr::planner::json::serialize(
      amr::planner::validator_json::result_to_value(validator::validate_fleet_plan(request)));
  expect(plain.find("\"stl\":null") != std::string::npos, "不传规约时 stl 序列化为 null");
}

void test_performance() {
  // 4 台 AMR、时间域 2000（规则层上限）、长路径：STL 监控开销必须远小于模型调用。
  const auto spec = load_spec_file();
  FleetPlanRequest request;
  request.environment_ref = "warehouse-perf";
  request.map.width = 30;
  request.map.height = 20;
  request.start_time = 0;
  request.max_time = 2000;
  for (int x = 8; x < 30; x += 5) {
    for (int y = 0; y < 20; ++y) {
      if (y % 3 != 0) request.map.blocked_cells.push_back(GridPosition{x, y});
    }
  }
  for (int index = 0; index < 4; ++index) {
    const std::string amr = "A" + std::to_string(index + 1);
    const std::string order = "O" + std::to_string(index + 1);
    const int row = index * 3;
    request.amrs.push_back(make_amr(amr, GridPosition{0, row}));
    request.orders.push_back(make_order(order, "P" + amr, "D" + amr));
    request.orders.back().deadline = 2000;
    request.locations.push_back(Location{"P" + amr, GridPosition{1, row}});
    request.locations.push_back(Location{"D" + amr, GridPosition{7, row}});
    std::vector<RouteStep> path = {step({0, row}, 90, 0, RouteAction::kStart, 0.0)};
    int time = 1;
    for (int x = 1; x <= 7; ++x) path.push_back(step({x, row}, 90, time++, RouteAction::kMove, time));
    while (time <= 1900) path.push_back(step({7, row}, 90, time++, RouteAction::kWait, time));
    request.routes.push_back(make_route(amr, order, 1.0, 1, time - 1, path));
  }
  const auto started = std::chrono::steady_clock::now();
  const auto result = validator::validate_fleet_plan(request, &spec);
  const auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                              std::chrono::steady_clock::now() - started)
                              .count();
  expect(result.valid && result.stl.has_value() && result.stl->satisfied,
         "性能用例的长计划必须合法且满足 STL");
  std::cout << "performance_ms=" << elapsed_ms << " instances=" << result.stl->instance_count << '\n';
  expect(elapsed_ms < 2000, "时间域 2000 的 STL 监控必须在 2 秒内完成");
}

void run_case(const std::string& name) {
  if (name == "parse_roundtrip") return test_parse_roundtrip();
  if (name == "atom_semantics") return test_atom_semantics();
  if (name == "boolean_operators") return test_boolean_operators();
  if (name == "globally_eventually") return test_globally_eventually();
  if (name == "until_semantics") return test_until_semantics();
  if (name == "nested_temporal") return test_nested_temporal();
  if (name == "spec_load_rejects") return test_spec_load_rejects();
  if (name == "spec_file") return test_spec_file();
  if (name == "fleet_valid_plan") return test_fleet_valid_plan();
  if (name == "fleet_violations") return test_fleet_violations();
  if (name == "boolean_consistency") return test_boolean_consistency();
  if (name == "gate_vs_shadow") return test_gate_vs_shadow();
  if (name == "stable_evidence") return test_stable_evidence();
  if (name == "performance") return test_performance();
  throw TestFailure("unknown test case: " + name);
}

}  // namespace

int main(int argc, char* argv[]) {
  std::string case_name;
  for (int index = 1; index < argc; ++index) {
    const std::string argument = argv[index];
    if (argument == "--case" && index + 1 < argc) {
      case_name = argv[++index];
    } else if (argument == "--spec" && index + 1 < argc) {
      g_spec_path = argv[++index];
    } else {
      std::cerr << "usage: stl_monitor_tests --case <name> [--spec <path>]\n";
      return 2;
    }
  }
  if (case_name.empty()) {
    std::cerr << "usage: stl_monitor_tests --case <name> [--spec <path>]\n";
    return 2;
  }
  try {
    run_case(case_name);
    std::cout << "case=" << case_name << " status=ok\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "case=" << case_name << " status=failed error=" << error.what() << '\n';
    return 1;
  }
}
