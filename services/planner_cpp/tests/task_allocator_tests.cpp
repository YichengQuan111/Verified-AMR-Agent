#include "task_allocator/task_allocator.hpp"
#include "task_allocator/json_codec.hpp"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using amr::planner::AMRState;
using amr::planner::AMRTaskStatus;
using amr::planner::AllocationConfig;
using amr::planner::AllocationError;
using amr::planner::AllocationRequest;
using amr::planner::AllocationResult;
using amr::planner::ConnectionStatus;
using amr::planner::CostWeights;
using amr::planner::GridPosition;
using amr::planner::HealthStatus;
using amr::planner::Location;
using amr::planner::TransportOrder;

void expect(bool condition, const std::string& message) {
  if (!condition) {
    throw std::runtime_error(message);
  }
}

AMRState make_amr(const std::string& id, int x, int y, double battery = 90.0) {
  return AMRState{id,
                  GridPosition{x, y},
                  0,
                  battery,
                  0.0,
                  AMRTaskStatus::kIdle,
                  HealthStatus::kHealthy,
                  ConnectionStatus::kOnline};
}

TransportOrder make_order(const std::string& id, const std::string& pickup,
                          const std::string& dropoff, int priority = 3) {
  return TransportOrder{id, "MAT-" + id, pickup, dropoff, priority, 0, 100, {}};
}

AllocationRequest make_request(std::vector<AMRState> amrs,
                               std::vector<TransportOrder> orders) {
  AllocationRequest request;
  request.amrs = std::move(amrs);
  request.orders = std::move(orders);
  request.locations = {
      Location{"P1", GridPosition{1, 0}},
      Location{"P2", GridPosition{9, 0}},
      Location{"P3", GridPosition{20, 0}},
      Location{"S1", GridPosition{2, 0}},
      Location{"S2", GridPosition{8, 0}},
      Location{"S3", GridPosition{21, 0}},
  };
  request.weights = CostWeights{1.0, 0.0, 0.0, 0.0, 0.0};
  request.config = AllocationConfig{};
  return request;
}

const amr::planner::PairEvaluation& find_pair(const AllocationResult& result,
                                              const std::string& amr_id,
                                              const std::string& order_id) {
  const auto amr_it = std::find(result.amr_ids.begin(), result.amr_ids.end(), amr_id);
  const auto order_it = std::find(result.order_ids.begin(), result.order_ids.end(), order_id);
  expect(amr_it != result.amr_ids.end() && order_it != result.order_ids.end(),
         "pair IDs were not present in normalized result");
  return result.pair_evaluations[static_cast<std::size_t>(amr_it - result.amr_ids.begin())]
                                [static_cast<std::size_t>(order_it - result.order_ids.begin())];
}

void test_normal() {
  AllocationRequest request = make_request(
      {make_amr("AMR-02", 10, 0), make_amr("AMR-01", 0, 0)},
      {make_order("ORDER-002", "P2", "S2"), make_order("ORDER-001", "P1", "S1")});
  const AllocationResult result = amr::planner::allocate_hungarian(request);
  expect(result.status == "complete", "normal allocation must be complete");
  expect(result.assignments.size() == 2, "normal allocation must assign two pairs");
  expect(result.assignments[0].amr_id == "AMR-01" && result.assignments[0].order_id == "ORDER-001",
         "normal allocation must select the nearby first AMR");
  expect(result.assignments[1].amr_id == "AMR-02" && result.assignments[1].order_id == "ORDER-002",
         "normal allocation must select the nearby second AMR");
  expect(find_pair(result, "AMR-01", "ORDER-001").feasible,
         "normal pair must be marked feasible");
  expect(find_pair(result, "AMR-01", "ORDER-001").components->distance_to_pickup == 1.0,
         "distance component must be reported");

  const AllocationResult baseline = amr::planner::allocate_nearest_idle(request);
  expect(baseline.algorithm == "nearest_idle_amr", "baseline must identify its own algorithm");
  expect(baseline.status == "complete" && baseline.assignments.size() == 2,
         "nearest idle baseline must solve the normal case");
}

void test_low_battery() {
  AllocationRequest request = make_request(
      {make_amr("AMR-20", 0, 0, 20.0), make_amr("AMR-25", 0, 0, 25.0)},
      {make_order("ORDER-LOW", "P1", "S1")});
  request.config.energy_per_cell_percent = 5.0;
  const AllocationResult result = amr::planner::allocate_hungarian(request);
  expect(result.status == "complete" && result.assignments.size() == 1,
         "a safe low-battery alternative must be selected");
  expect(result.assignments.front().amr_id == "AMR-25", "20% AMR must not receive a new order");
  const auto& blocked = find_pair(result, "AMR-20", "ORDER-LOW");
  expect(!blocked.feasible && blocked.cost >= amr::planner::kInternalInf,
         "20% AMR pair must use internal INF");
  expect(std::find(blocked.reason_codes.begin(), blocked.reason_codes.end(),
                   "battery_below_new_task_threshold") != blocked.reason_codes.end(),
         "low-battery reason code must be stable");
  const auto& warning = find_pair(result, "AMR-25", "ORDER-LOW");
  expect(warning.components->estimated_battery_after == 15.0,
         "battery completion estimate must use the full transport route");
  expect(warning.components->battery_risk > 0.0,
         "the warning interval must contribute battery risk");
}

void test_no_feasible() {
  AllocationRequest request = make_request(
      {make_amr("AMR-OFF", 0, 0, 5.0)}, {make_order("ORDER-NONE", "P1", "S1")});
  request.amrs.front().task_status = AMRTaskStatus::kOffline;
  request.amrs.front().connection_status = ConnectionStatus::kOffline;
  const AllocationResult result = amr::planner::allocate_hungarian(request);
  expect(result.status == "no_feasible_assignment", "all-INF allocation must be explicit");
  expect(result.assignments.empty(), "all-INF allocation must not emit a fake assignment");
  expect(result.unassigned_orders.size() == 1 &&
             result.unassigned_orders.front().reason_code == "no_feasible_amr",
         "no-feasible order reason must be stable");
  const auto& pair = find_pair(result, "AMR-OFF", "ORDER-NONE");
  expect(!pair.feasible && pair.reason_codes.size() >= 3,
         "offline and critical battery causes must be retained");
}

void test_more_orders() {
  AllocationRequest request = make_request(
      {make_amr("AMR-01", 0, 0)},
      {make_order("ORDER-001", "P1", "S1"), make_order("ORDER-002", "P2", "S2")});
  const AllocationResult result = amr::planner::allocate_hungarian(request);
  expect(result.status == "partial" && result.assignments.size() == 1,
         "orders greater than vehicles must produce a partial allocation");
  expect(result.unassigned_orders.size() == 1 &&
             result.unassigned_orders.front().reason_code == "capacity_exhausted",
         "unmatched order must identify vehicle capacity as the reason");
  expect(result.pair_evaluations.size() == 1 && result.pair_evaluations.front().size() == 2,
         "rectangular pair matrix dimensions must be preserved");
}

void test_edge_cases() {
  AllocationRequest tie_request = make_request(
      {make_amr("AMR-02", 0, 0), make_amr("AMR-01", 0, 0)},
      {make_order("ORDER-TIE", "P1", "S1")});
  const AllocationResult tie_result = amr::planner::allocate_hungarian(tie_request);
  expect(tie_result.assignments.size() == 1 && tie_result.assignments.front().amr_id == "AMR-01",
         "equal-cost ties must use lexicographically stable AMR IDs");

  AllocationRequest reserve_request = make_request(
      {make_amr("AMR-24", 0, 0, 24.0)}, {make_order("ORDER-RESERVE", "P1", "S1")});
  reserve_request.config.energy_per_cell_percent = 5.0;
  const AllocationResult reserve_result = amr::planner::allocate_hungarian(reserve_request);
  expect(!find_pair(reserve_result, "AMR-24", "ORDER-RESERVE").feasible,
         "completion below 15% reserve must be infeasible");
  expect(std::find(find_pair(reserve_result, "AMR-24", "ORDER-RESERVE").reason_codes.begin(),
                   find_pair(reserve_result, "AMR-24", "ORDER-RESERVE").reason_codes.end(),
                   "completion_below_safety_reserve") !=
             find_pair(reserve_result, "AMR-24", "ORDER-RESERVE").reason_codes.end(),
         "reserve violation must return an explicit reason");

  AllocationRequest dependency_request = make_request(
      {make_amr("AMR-01", 0, 0)},
      {make_order("ORDER-DEP", "P1", "S1"),
       TransportOrder{"ORDER-CHILD", "MAT-CHILD", "P1", "S1", 3, 0, 100, {"ORDER-DEP"}}});
  const AllocationResult dependency_result = amr::planner::allocate_hungarian(dependency_request);
  const auto& dependency_pair = find_pair(dependency_result, "AMR-01", "ORDER-CHILD");
  expect(!dependency_pair.feasible &&
             std::find(dependency_pair.reason_codes.begin(), dependency_pair.reason_codes.end(),
                       "order_dependency_pending") != dependency_pair.reason_codes.end(),
         "pending dependency must block the pair deterministically");

  AllocationRequest invalid = make_request(
      {make_amr("AMR-01", 0, 0), make_amr("AMR-01", 1, 0)},
      {make_order("ORDER-1", "P1", "S1")});
  bool rejected = false;
  try {
    (void)amr::planner::allocate_hungarian(invalid);
  } catch (const AllocationError& error) {
    rejected = error.code() == "invalid_request";
  }
  expect(rejected, "duplicate AMR IDs must be rejected before matching");

  AllocationRequest cyclic = make_request(
      {make_amr("AMR-CYCLE", 0, 0)},
      {TransportOrder{"ORDER-A", "MAT-A", "P1", "S1", 3, 0, 100, {"ORDER-B"}},
       TransportOrder{"ORDER-B", "MAT-B", "P1", "S1", 3, 0, 100, {"ORDER-A"}}});
  bool cycle_rejected = false;
  try {
    (void)amr::planner::allocate_hungarian(cyclic);
  } catch (const AllocationError& error) {
    cycle_rejected = error.code() == "invalid_request";
  }
  expect(cycle_rejected, "cyclic order dependencies must be rejected before matching");

  AllocationRequest cost_request = make_request(
      {make_amr("AMR-COST", 0, 0)}, {make_order("ORDER-COST", "P1", "S1", 5)});
  cost_request.amrs.front().load = 50.0;
  cost_request.orders.front().deadline = 1;
  cost_request.weights = CostWeights{1.0, 1.0, 1.0, 1.0, 1.0};
  const AllocationResult cost_result = amr::planner::allocate_hungarian(cost_request);
  const auto& cost_pair = find_pair(cost_result, "AMR-COST", "ORDER-COST");
  expect(cost_pair.feasible, "lateness risk is a cost signal, not an implicit INF rule");
  expect(cost_pair.components->lateness_risk > 0.0,
         "late completion must contribute lateness risk");
  expect(cost_pair.components->load_penalty == 0.5,
         "current load must contribute load penalty");
  expect(cost_pair.components->priority_bonus == 1.0,
         "priority five must produce the maximum priority bonus");
}

void test_json_contract() {
  const std::string request_text = R"json({
    "schema_version":"1.0",
    "amrs":[{"amr_id":"AMR-01","position":{"x":0,"y":0},"heading":0,"battery":90,"load":0,"task_status":"IDLE","health_status":"HEALTHY","connection_status":"ONLINE"}],
    "orders":[{"order_id":"ORDER-JSON","material_id":"MAT-JSON","pickup":"P1","dropoff":"S1","priority":5,"release_time":0,"deadline":100,"dependencies":[]}],
    "location_positions":{"P1":{"x":1,"y":0},"S1":{"x":2,"y":0}},
    "completed_order_ids":[],
    "weights":{"distance":1,"lateness_risk":10,"battery_risk":5,"load_penalty":2,"priority_bonus":1},
    "config":{"current_time":0,"maximum_load_kg":100,"travel_speed_cells_per_second":1,"energy_per_cell_percent":1,"battery_warning_threshold_percent":30,"new_task_battery_threshold_percent":20,"critical_battery_threshold_percent":10,"battery_safety_reserve_percent":15}
  })json";
  const auto request = amr::planner::json::request_from_value(
      amr::planner::json::parse(request_text));
  const auto result = amr::planner::allocate_hungarian(request);
  const std::string response = amr::planner::json::serialize(
      amr::planner::json::result_to_value(result));
  expect(response.find("\"algorithm\":\"hungarian\"") != std::string::npos,
         "JSON response must identify the production algorithm");
  expect(response.find("\"status\":\"complete\"") != std::string::npos,
         "JSON response must preserve allocation status");
  const auto reparsed = amr::planner::json::parse(response);
  expect(reparsed.is_object(), "serialized response must be valid JSON object");

  bool duplicate_rejected = false;
  try {
    (void)amr::planner::json::parse("{\"a\":1,\"a\":2}");
  } catch (const amr::planner::json::ParseError&) {
    duplicate_rejected = true;
  }
  expect(duplicate_rejected, "JSON duplicate keys must not be silently overwritten");

  AllocationRequest no_feasible = request;
  no_feasible.amrs.front().battery = 5.0;
  const auto no_feasible_result = amr::planner::allocate_hungarian(no_feasible);
  const std::string no_feasible_json = amr::planner::json::serialize(
      amr::planner::json::result_to_value(no_feasible_result));
  expect(no_feasible_json.find("\"INF\"") != std::string::npos,
         "infeasible JSON matrix must use the stable INF sentinel");
}

void run_case(const std::string& name) {
  if (name == "normal") {
    test_normal();
  } else if (name == "low_battery") {
    test_low_battery();
  } else if (name == "no_feasible") {
    test_no_feasible();
  } else if (name == "more_orders") {
    test_more_orders();
  } else if (name == "edge_cases") {
    test_edge_cases();
  } else if (name == "json_contract") {
    test_json_contract();
  } else {
    throw std::runtime_error("unknown test case: " + name);
  }
}

}  // namespace

int main(int argc, char* argv[]) {
  try {
    if (argc == 3 && std::string(argv[1]) == "--case") {
      run_case(argv[2]);
      std::cout << "{\"case\":\"" << argv[2] << "\",\"status\":\"ok\"}\n";
      return 0;
    }
    if (argc == 2 && std::string(argv[1]) == "--all") {
      for (const auto& name : {"normal", "low_battery", "no_feasible", "more_orders", "edge_cases", "json_contract"}) {
        run_case(name);
      }
      std::cout << "{\"case\":\"all\",\"status\":\"ok\"}\n";
      return 0;
    }
    std::cerr << "Usage: task_allocator_tests --case <normal|low_battery|no_feasible|more_orders|edge_cases|json_contract>\n";
    return 2;
  } catch (const std::exception& error) {
    std::cerr << "{\"status\":\"failed\",\"message\":\"" << error.what() << "\"}\n";
    return 1;
  }
}
