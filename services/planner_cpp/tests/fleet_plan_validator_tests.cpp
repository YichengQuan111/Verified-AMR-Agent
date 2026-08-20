#include "fleet_plan_validator/fleet_plan_validator.hpp"
#include "fleet_plan_validator/json_codec.hpp"

#include <algorithm>
#include <cmath>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

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
using validator::FleetPlanRoute;
using validator::FleetPlanRequest;

class TestFailure final : public std::runtime_error {
 public:
  explicit TestFailure(const std::string& message) : std::runtime_error(message) {}
};

void expect(bool condition, const std::string& message) {
  if (!condition) throw TestFailure(message);
}

AMRState make_amr(const std::string& id, GridPosition position, int heading = 90,
                 double battery = 100.0) {
  return AMRState{id, position, heading, battery, 0.0, AMRTaskStatus::kIdle,
                  HealthStatus::kHealthy, ConnectionStatus::kOnline};
}

TransportOrder make_order(const std::string& id, const std::string& pickup,
                          const std::string& dropoff) {
  return TransportOrder{id, "material-" + id, pickup, dropoff, 3, 0, 20, {}};
}

RouteStep step(GridPosition position, int heading, int time, RouteAction action,
               double g_cost) {
  return RouteStep{position, heading, time, action, g_cost};
}

FleetPlanRoute make_route(const std::string& amr_id, const std::string& order_id,
                          double payload, int pickup_time, int dropoff_time,
                          std::vector<RouteStep> path) {
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
  request.amrs = {
      make_amr("A1", GridPosition{0, 0}),
      make_amr("A2", GridPosition{0, 4}),
  };
  request.orders = {
      make_order("O1", "P1", "D1"),
      make_order("O2", "P2", "D2"),
  };
  request.locations = {
      Location{"P1", GridPosition{1, 0}},
      Location{"D1", GridPosition{2, 0}},
      Location{"P2", GridPosition{1, 4}},
      Location{"D2", GridPosition{2, 4}},
  };
  request.workstation_capacities = {
      {"P1", 1}, {"D1", 1}, {"P2", 1}, {"D2", 1},
  };
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

bool has_code(const validator::ValidationResult& result, const std::string& code) {
  return std::any_of(result.errors.begin(), result.errors.end(),
                     [&](const auto& error) { return error.code == code; });
}

const validator::ValidationEvidence& find_error(const validator::ValidationResult& result,
                                                const std::string& code) {
  for (const auto& error : result.errors) {
    if (error.code == code) return error;
  }
  throw TestFailure("missing expected error code: " + code);
}

void expect_located(const validator::ValidationResult& result, const std::string& code) {
  const auto& error = find_error(result, code);
  expect(!error.constraint.empty(), code + " must identify its constraint");
  expect(!error.message.empty(), code + " must have a message");
  expect(!error.order_id.empty() || !error.amr_id.empty(),
         code + " must identify an order/task or AMR");
  expect(error.coordinate.has_value() || error.time.has_value(),
         code + " must include a coordinate or time when the rule has a location");
}

void test_valid() {
  const auto first = validator::validate_fleet_plan(base_request());
  const auto second = validator::validate_fleet_plan(base_request());
  expect(first.valid && first.status == "valid", "safe fleet plan must pass");
  expect(first.errors.empty(), "safe fleet plan must not contain errors");
  expect(second.valid && second.status == "valid", "safe fleet plan must be repeatable");
}

void test_dependency() {
  auto request = base_request();
  request.orders[1].dependencies = {"O1"};
  const auto result = validator::validate_fleet_plan(request);
  expect(has_code(result, "task_dependency_time_order"),
         "dependency pickup before predecessor dropoff must be rejected");
  expect_located(result, "task_dependency_time_order");
}

void test_time_window() {
  auto request = base_request();
  request.orders[0].release_time = 3;
  request.orders[0].deadline = 1;
  const auto result = validator::validate_fleet_plan(request);
  expect(has_code(result, "invalid_order"), "invalid order time window must be rejected");
  expect(has_code(result, "pickup_before_release"), "early pickup must be located");
  expect(has_code(result, "dropoff_after_deadline"), "late dropoff must be located");
  expect_located(result, "pickup_before_release");
  expect_located(result, "dropoff_after_deadline");
}

void test_load() {
  auto request = base_request();
  request.routes[0].payload_kg = 101.0;
  const auto result = validator::validate_fleet_plan(request);
  expect(has_code(result, "load_capacity_exceeded"), "payload over capacity must be rejected");
  expect_located(result, "load_capacity_exceeded");
}

void test_battery() {
  auto request = base_request();
  request.amrs[0].battery = 16.0;
  const auto result = validator::validate_fleet_plan(request);
  expect(has_code(result, "battery_safety_reserve_breached"),
         "route ending below reserve must be rejected");
  expect_located(result, "battery_safety_reserve_breached");
}

void test_forbidden_zone() {
  auto request = base_request();
  request.map.blocked_cells = {GridPosition{1, 0}};
  const auto result = validator::validate_fleet_plan(request);
  expect(has_code(result, "forbidden_zone_occupied"),
         "path entering blocked cell must be rejected");
  expect_located(result, "forbidden_zone_occupied");
}

void test_workstation_capacity() {
  auto request = base_request();
  request.amrs = {
      make_amr("A1", GridPosition{0, 1}, 90),
      make_amr("A2", GridPosition{2, 1}, 270),
  };
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
  const auto result = validator::validate_fleet_plan(request);
  expect(has_code(result, "workstation_capacity_exceeded"),
         "simultaneous station service must be capacity checked");
  expect_located(result, "workstation_capacity_exceeded");

  request = base_request();
  request.workstation_capacities["P1"] = 0;
  const auto invalid_capacity = validator::validate_fleet_plan(request);
  expect(has_code(invalid_capacity, "workstation_capacity_config_missing"),
         "non-positive workstation capacity must be rejected even without a service event");
  expect_located(invalid_capacity, "workstation_capacity_config_missing");
}

void test_safety_distance() {
  auto request = base_request();
  request.config.minimum_safety_distance_cells = 2;
  request.amrs = {
      make_amr("A1", GridPosition{0, 0}, 90),
      make_amr("A2", GridPosition{1, 0}, 90),
  };
  request.orders = {
      make_order("O1", "P1", "D1"),
      make_order("O2", "P2", "D2"),
  };
  request.locations = {
      Location{"P1", GridPosition{0, 0}}, Location{"D1", GridPosition{0, 1}},
      Location{"P2", GridPosition{1, 0}}, Location{"D2", GridPosition{1, 1}},
  };
  request.workstation_capacities = {{"P1", 1}, {"D1", 1}, {"P2", 1}, {"D2", 1}};
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
  const auto result = validator::validate_fleet_plan(request);
  expect(has_code(result, "safety_distance_breached"),
         "insufficient configured safety distance must be rejected");
  expect(!has_code(result, "vertex_conflict"),
         "adjacent safety violation must remain distinguishable from vertex conflict");
  expect_located(result, "safety_distance_breached");
}

void test_vertex_conflict() {
  auto request = base_request();
  request.amrs = {
      make_amr("A1", GridPosition{0, 0}, 90),
      make_amr("A2", GridPosition{2, 0}, 270),
  };
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
  const auto result = validator::validate_fleet_plan(request);
  expect(has_code(result, "vertex_conflict"), "same cell at same time must be rejected");
  expect_located(result, "vertex_conflict");
}

void test_swap_edge_conflict() {
  auto request = base_request();
  request.amrs = {
      make_amr("A1", GridPosition{0, 1}, 90),
      make_amr("A2", GridPosition{1, 1}, 270),
  };
  request.orders = {
      make_order("O1", "P1", "D1"),
      make_order("O2", "P2", "D2"),
  };
  request.locations = {
      Location{"P1", GridPosition{1, 1}}, Location{"D1", GridPosition{1, 2}},
      Location{"P2", GridPosition{0, 1}}, Location{"D2", GridPosition{0, 2}},
  };
  request.workstation_capacities = {{"P1", 1}, {"D1", 1}, {"P2", 1}, {"D2", 1}};
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
  const auto result = validator::validate_fleet_plan(request);
  expect(has_code(result, "swap_edge_conflict"), "swapping an edge must be rejected");
  expect_located(result, "swap_edge_conflict");
}

void test_route_geometry() {
  auto request = base_request();
  request.map.blocked_edges = {RouteEdge{GridPosition{0, 0}, GridPosition{1, 0}}};
  const auto blocked = validator::validate_fleet_plan(request);
  expect(has_code(blocked, "forbidden_edge_traversed"),
         "blocked edge traversal must be rejected");
  expect_located(blocked, "forbidden_edge_traversed");

  request = base_request();
  request.map.one_way_edges = {RouteEdge{GridPosition{1, 0}, GridPosition{0, 0}}};
  const auto one_way = validator::validate_fleet_plan(request);
  expect(has_code(one_way, "one_way_violation"),
         "reverse traversal of one-way edge must be rejected");
  expect_located(one_way, "one_way_violation");

  request = base_request();
  // 仅仅移动到相邻格并不等于合法前进；位置变化必须和 RouteStep.heading 一致。
  request.routes[0].path[1] = step({0, 1}, 90, 1, RouteAction::kMove, 1.0);
  const auto wrong_heading = validator::validate_fleet_plan(request);
  expect(has_code(wrong_heading, "route_action_invalid"),
         "move direction must match the declared heading");
}

void test_stable_evidence() {
  auto request = base_request();
  request.map.blocked_cells = {GridPosition{1, 0}};
  const auto first = validator::validate_fleet_plan(request);
  const auto second = validator::validate_fleet_plan(request);
  const auto first_json = amr::planner::json::serialize(
      amr::planner::validator_json::result_to_value(first));
  const auto second_json = amr::planner::json::serialize(
      amr::planner::validator_json::result_to_value(second));
  expect(first_json == second_json, "same invalid plan must produce byte-identical evidence");
  expect(!first.valid && first.status == "invalid", "invalid plan must not be a boolean-only result");
  expect(first_json.find("forbidden_zone_occupied") != std::string::npos,
         "serialized evidence must contain stable error code");
}

void test_json_contract() {
  bool rejected = false;
  try {
    const auto value = amr::planner::json::parse(
        R"({"schema_version":"1.0","llm_valid":true})");
    (void)amr::planner::validator_json::request_from_value(value);
  } catch (const amr::planner::json::ParseError&) {
    rejected = true;
  }
  expect(rejected, "Validator JSON must reject LLM bypass fields");
}

void test_error_dictionary() {
  const auto& definitions = validator::error_dictionary();
  expect(!definitions.empty(), "error dictionary must not be empty");
  std::set<std::string> codes;
  for (std::size_t index = 0; index < definitions.size(); ++index) {
    expect(codes.insert(definitions[index].code).second, "error codes must be unique");
    expect(!definitions[index].constraint.empty(), "error constraint must be documented");
    if (index > 0) {
      expect(definitions[index - 1].code < definitions[index].code,
             "error dictionary must be sorted for stable lookup");
    }
  }
  for (const std::string required : {"task_dependency_time_order", "pickup_before_release",
                                     "load_capacity_exceeded", "battery_safety_reserve_breached",
                                     "forbidden_zone_occupied", "forbidden_edge_traversed",
                                     "workstation_capacity_exceeded",
                                     "safety_distance_breached", "vertex_conflict",
                                     "swap_edge_conflict"}) {
    expect(codes.count(required) == 1U, "error dictionary missing " + required);
  }
}

void run_case(const std::string& name) {
  if (name == "valid") return test_valid();
  if (name == "dependency") return test_dependency();
  if (name == "time_window") return test_time_window();
  if (name == "load") return test_load();
  if (name == "battery") return test_battery();
  if (name == "forbidden_zone") return test_forbidden_zone();
  if (name == "workstation_capacity") return test_workstation_capacity();
  if (name == "safety_distance") return test_safety_distance();
  if (name == "vertex_conflict") return test_vertex_conflict();
  if (name == "swap_edge_conflict") return test_swap_edge_conflict();
  if (name == "route_geometry") return test_route_geometry();
  if (name == "stable_evidence") return test_stable_evidence();
  if (name == "json_contract") return test_json_contract();
  if (name == "error_dictionary") return test_error_dictionary();
  throw TestFailure("unknown test case: " + name);
}

}  // namespace

int main(int argc, char* argv[]) {
  if (argc != 3 || std::string(argv[1]) != "--case") {
    std::cerr << "usage: fleet_plan_validator_tests --case <name>\n";
    return 2;
  }
  try {
    run_case(argv[2]);
    std::cout << "case=" << argv[2] << " status=ok\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "case=" << argv[2] << " status=failed error=" << error.what() << '\n';
    return 1;
  }
}
