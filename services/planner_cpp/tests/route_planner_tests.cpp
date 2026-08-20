#include "route_planner/json_codec.hpp"
#include "route_planner/route_planner.hpp"

#include <chrono>
#include <cmath>
#include <iostream>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>

namespace {

using amr::planner::AMRState;
using amr::planner::AMRTaskStatus;
using amr::planner::ConnectionStatus;
using amr::planner::GridPosition;
using amr::planner::HealthStatus;
using amr::planner::Location;
using amr::planner::PlannedRoute;
using amr::planner::RouteAction;
using amr::planner::RouteAssignment;
using amr::planner::RouteEdge;
using amr::planner::RouteError;
using amr::planner::RoutePlanResult;
using amr::planner::RouteRequest;
using amr::planner::RouteStep;
using amr::planner::TransportOrder;

struct TestFailure final : std::runtime_error {
  using std::runtime_error::runtime_error;
};

void expect(bool condition, const std::string& message) {
  if (!condition) throw TestFailure(message);
}

AMRState make_amr(const std::string& id, GridPosition position, int heading) {
  return AMRState{
      id,
      position,
      heading,
      100.0,
      0.0,
      AMRTaskStatus::kIdle,
      HealthStatus::kHealthy,
      ConnectionStatus::kOnline,
  };
}

TransportOrder make_order(const std::string& id,
                          const std::string& pickup,
                          const std::string& dropoff,
                          int priority = 3,
                          int release_time = 0) {
  return TransportOrder{
      id,
      "material-" + id,
      pickup,
      dropoff,
      priority,
      release_time,
      200,
      {},
  };
}

RouteRequest one_route_request(int width = 7, int height = 5) {
  RouteRequest request;
  request.environment_ref = "test-environment";
  request.map.width = width;
  request.map.height = height;
  request.max_time = 80;
  request.amrs = {make_amr("A1", GridPosition{0, height / 2}, 90)};
  request.orders = {make_order("O1", "P1", "D1")};
  request.locations = {
      Location{"P1", GridPosition{2, height / 2}},
      Location{"D1", GridPosition{width - 2, height / 2}},
  };
  request.assignments = {RouteAssignment{"A1", "O1"}};
  return request;
}

const RouteStep& step_at(const PlannedRoute& route, int time) {
  for (const auto& step : route.path) {
    if (step.time == time) return step;
  }
  throw TestFailure("route does not contain expected time step");
}

GridPosition position_at(const PlannedRoute& route, int time) {
  if (route.path.empty()) throw TestFailure("planned route must not be empty");
  if (time <= route.path.front().time) return route.path.front().position;
  for (const auto& step : route.path) {
    if (step.time == time) return step.position;
  }
  return route.path.back().position;
}

bool has_action(const PlannedRoute& route, RouteAction action) {
  for (const auto& step : route.path) {
    if (step.action == action) return true;
  }
  return false;
}

bool paths_equal(const std::vector<RouteStep>& left, const std::vector<RouteStep>& right) {
  if (left.size() != right.size()) return false;
  for (std::size_t index = 0; index < left.size(); ++index) {
    const auto& a = left[index];
    const auto& b = right[index];
    if (a.position.x != b.position.x || a.position.y != b.position.y ||
        a.heading != b.heading || a.time != b.time || a.action != b.action ||
        std::abs(a.g_cost - b.g_cost) >= 1.0e-9) {
      return false;
    }
  }
  return true;
}

void assert_path_safe(const RouteRequest& request, const PlannedRoute& route) {
  expect(route.status == "planned", "expected a planned route");
  expect(!route.path.empty(), "planned route path must not be empty");
  std::set<std::pair<int, int>> blocked;
  for (const auto& cell : request.map.blocked_cells) blocked.emplace(cell.x, cell.y);

  expect(route.path.front().time == request.start_time, "path must start at request start_time");
  for (std::size_t index = 0; index < route.path.size(); ++index) {
    const auto& step = route.path[index];
    expect(step.position.x >= 0 && step.position.x < request.map.width &&
               step.position.y >= 0 && step.position.y < request.map.height,
           "path left map boundary");
    expect(blocked.count({step.position.x, step.position.y}) == 0U,
           "path entered a blocked cell");
    expect(step.heading == 0 || step.heading == 90 || step.heading == 180 || step.heading == 270,
           "path emitted an invalid heading");
    if (index == 0) {
      expect(step.action == RouteAction::kStart, "first path action must be start");
      continue;
    }
    const auto& previous = route.path[index - 1];
    expect(step.time == previous.time + 1, "path time must be contiguous");
    const int distance = std::abs(step.position.x - previous.position.x) +
                         std::abs(step.position.y - previous.position.y);
    if (step.action == RouteAction::kMove) {
      expect(distance == 1 && step.heading == previous.heading,
             "move action must advance one cell without changing heading");
      for (const auto& edge : request.map.blocked_edges) {
        expect(!(edge.from.x == previous.position.x && edge.from.y == previous.position.y &&
                 edge.to.x == step.position.x && edge.to.y == step.position.y),
               "path used a blocked edge");
      }
      for (const auto& edge : request.map.one_way_edges) {
        const bool reverse_only = edge.from.x == step.position.x &&
                                  edge.from.y == step.position.y &&
                                  edge.to.x == previous.position.x &&
                                  edge.to.y == previous.position.y;
        const bool forward_is_listed = edge.from.x == previous.position.x &&
                                       edge.from.y == previous.position.y &&
                                       edge.to.x == step.position.x &&
                                       edge.to.y == step.position.y;
        expect(!reverse_only || forward_is_listed, "path violated a one-way edge");
      }
    } else if (step.action == RouteAction::kTurnLeft ||
               step.action == RouteAction::kTurnRight ||
               step.action == RouteAction::kWait) {
      expect(distance == 0, "turn/wait action must keep the same cell");
    } else {
      throw TestFailure("only the first path step may use start action");
    }
    expect(step.g_cost + 1.0e-9 >= previous.g_cost,
           "path g cost must be monotonic");
  }
}

void assert_no_fleet_conflicts(const RoutePlanResult& result, int max_time) {
  for (int time = 0; time <= max_time; ++time) {
    for (std::size_t left = 0; left < result.routes.size(); ++left) {
      if (result.routes[left].status != "planned") continue;
      const auto left_cell = position_at(result.routes[left], time);
      for (std::size_t right = left + 1; right < result.routes.size(); ++right) {
        if (result.routes[right].status != "planned") continue;
        const auto right_cell = position_at(result.routes[right], time);
        expect(left_cell.x != right_cell.x || left_cell.y != right_cell.y,
               "planned routes contain a vertex conflict");
        if (time == max_time) continue;
        const auto left_next = position_at(result.routes[left], time + 1);
        const auto right_next = position_at(result.routes[right], time + 1);
        expect(!(left_cell.x == right_next.x && left_cell.y == right_next.y &&
                 right_cell.x == left_next.x && right_cell.y == left_next.y &&
                 (left_cell.x != left_next.x || left_cell.y != left_next.y)),
               "planned routes contain a swap-edge conflict");
      }
    }
  }
}

void test_obstacles() {
  auto request = one_route_request();
  request.map.blocked_cells = {
      GridPosition{1, 2}, GridPosition{3, 2}, GridPosition{4, 2},
  };
  const auto result = amr::planner::plan_routes_astar(request);
  expect(result.status == "complete", "A* should route around obstacles");
  assert_path_safe(request, result.routes.front());
  expect(result.routes.front().path.back().position.x == 5 &&
             result.routes.front().path.back().position.y == 2,
         "route should end at dropoff");
  expect(has_action(result.routes.front(), RouteAction::kTurnLeft) ||
             has_action(result.routes.front(), RouteAction::kTurnRight),
         "obstacle route should include a turn");
}

void test_boundary() {
  auto request = one_route_request(4, 3);
  request.amrs = {make_amr("A1", GridPosition{0, 0}, 0)};
  request.locations = {
      Location{"P1", GridPosition{0, 0}},
      Location{"D1", GridPosition{3, 0}},
  };
  const auto result = amr::planner::plan_routes_astar(request);
  expect(result.status == "complete", "boundary route should remain feasible");
  assert_path_safe(request, result.routes.front());

  request.map.blocked_cells = {GridPosition{4, 0}};
  bool rejected = false;
  try {
    (void)amr::planner::plan_routes_astar(request);
  } catch (const RouteError& error) {
    rejected = error.code() == "position_out_of_bounds";
  }
  expect(rejected, "out-of-bounds blocked cells must be rejected");
}

void test_forbidden_edges() {
  auto request = one_route_request(6, 3);
  request.map.blocked_edges = {
      RouteEdge{GridPosition{1, 1}, GridPosition{2, 1}},
      RouteEdge{GridPosition{2, 1}, GridPosition{3, 1}},
  };
  const auto result = amr::planner::plan_routes_astar(request);
  expect(result.status == "complete", "blocked edges should allow a safe detour");
  assert_path_safe(request, result.routes.front());
  expect(has_action(result.routes.front(), RouteAction::kTurnLeft) ||
             has_action(result.routes.front(), RouteAction::kTurnRight),
         "blocked-edge detour should change heading");
}

void test_one_way_edges() {
  auto request = one_route_request(3, 1);
  request.amrs = {make_amr("A1", GridPosition{2, 0}, 270)};
  request.locations = {
      Location{"P1", GridPosition{1, 0}},
      Location{"D1", GridPosition{0, 0}},
  };
  // 只允许 0 -> 1；订单需要从 1 反向驶向 0，单行硬约束应使路线不可行。
  request.map.one_way_edges = {
      RouteEdge{GridPosition{0, 0}, GridPosition{1, 0}},
  };
  const auto result = amr::planner::plan_routes_astar(request);
  expect(result.status == "infeasible", "reverse traversal of a one-way edge must be infeasible");
  expect(result.routes.front().reason_code == "no_safe_path_to_dropoff",
         "one-way infeasibility must not be hidden as a successful route");
}

RouteRequest waiting_request() {
  RouteRequest request;
  request.environment_ref = "wait-and-conflict";
  request.map.width = 5;
  request.map.height = 3;
  request.max_time = 30;
  // 让“等待让行”与连续原地转向不再有更低代价，确保该场景验证的是
  // 预约冲突下的 wait 动作，而不是方向调整的偶然 tie-break。
  request.costs.turn_cost = 1.0;
  request.amrs = {
      make_amr("A1", GridPosition{0, 1}, 90),
      make_amr("A2", GridPosition{2, 0}, 90),
  };
  request.orders = {
      make_order("O1", "P1", "D1", 5),
      make_order("O2", "P2", "D2", 1),
  };
  request.locations = {
      Location{"P1", GridPosition{1, 1}},
      Location{"D1", GridPosition{4, 1}},
      Location{"P2", GridPosition{2, 1}},
      Location{"D2", GridPosition{2, 2}},
  };
  request.assignments = {
      RouteAssignment{"A1", "O1"},
      RouteAssignment{"A2", "O2"},
  };
  return request;
}

void test_wait_and_vertex_reservation() {
  const auto request = waiting_request();
  const auto result = amr::planner::plan_routes_astar(request);
  expect(result.status == "complete", "priority planner should find a waiting solution");
  expect(result.routes.size() == 2U && result.routes[0].order_id == "O1",
         "routes must be planned in priority order");
  for (const auto& route : result.routes) assert_path_safe(request, route);
  assert_no_fleet_conflicts(result, request.max_time);
  expect(has_action(result.routes[1], RouteAction::kWait),
         "lower-priority AMR must wait for the reserved vertex");
  expect(result.routes[1].pickup_time > 2,
         "lower-priority pickup should be delayed past the conflicting vertex");
}

void test_vertex_conflict_direct() {
  amr::planner::ReservationTable reservations(5);
  const std::vector<RouteStep> first = {
      RouteStep{GridPosition{0, 0}, 90, 0, RouteAction::kStart, 0.0},
      RouteStep{GridPosition{1, 0}, 90, 1, RouteAction::kMove, 1.0},
  };
  reservations.reserve_path(first, 5);
  expect(reservations.is_cell_reserved(GridPosition{1, 0}, 1),
         "reserve_path must register (cell,t)");
  expect(!reservations.can_transition(GridPosition{2, 0}, GridPosition{1, 0}, 0),
         "reserved target cell must reject a vertex conflict");
}

void test_swap_edge_conflict() {
  amr::planner::ReservationTable reservations(5);
  const std::vector<RouteStep> first = {
      RouteStep{GridPosition{0, 0}, 90, 0, RouteAction::kStart, 0.0},
      RouteStep{GridPosition{1, 0}, 90, 1, RouteAction::kMove, 1.0},
  };
  reservations.reserve_path(first, 5);
  expect(reservations.is_edge_reserved(GridPosition{0, 0}, GridPosition{1, 0}, 0),
         "reserve_path must register (edge,t)");
  expect(!reservations.can_transition(GridPosition{1, 0}, GridPosition{0, 0}, 0),
         "reverse edge must be rejected as a swap conflict");
}

void test_no_solution() {
  auto request = one_route_request(3, 1);
  request.amrs = {make_amr("A1", GridPosition{0, 0}, 90)};
  request.locations = {
      Location{"P1", GridPosition{0, 0}},
      Location{"D1", GridPosition{2, 0}},
  };
  request.map.blocked_cells = {GridPosition{1, 0}};
  const auto result = amr::planner::plan_routes_astar(request);
  expect(result.status == "infeasible", "blocked one-cell corridor must be infeasible");
  expect(result.routes.front().reason_code == "no_safe_path_to_dropoff",
         "infeasible result must identify the failed route segment");
  expect(result.routes.front().path.empty(), "infeasible route must not emit an unsafe path");
}

void test_dijkstra_baseline() {
  const auto request = one_route_request();
  const auto astar = amr::planner::plan_routes_astar(request);
  const auto dijkstra = amr::planner::plan_routes_dijkstra(request);
  expect(astar.status == "complete" && dijkstra.status == "complete",
         "both algorithms should solve the same safe request");
  expect(std::abs(astar.total_cost - dijkstra.total_cost) < 1.0e-9,
         "A* and Dijkstra should agree on optimal route cost");
  expect(paths_equal(astar.routes.front().path, dijkstra.routes.front().path),
         "deterministic direct route should match the independent baseline");
  expect(dijkstra.total_expanded_states >= astar.total_expanded_states,
         "Dijkstra should not use the A* heuristic to reduce expansions");
}

void test_reproducibility() {
  auto request = waiting_request();
  request.map.blocked_cells = {GridPosition{1, 0}, GridPosition{3, 2}};
  const auto first = amr::planner::plan_routes_astar(request);
  const auto second = amr::planner::plan_routes_astar(request);
  const auto first_json = amr::planner::json::serialize(
      amr::planner::route_json::result_to_value(first));
  const auto second_json = amr::planner::json::serialize(
      amr::planner::route_json::result_to_value(second));
  expect(first_json == second_json, "same request must produce byte-identical JSON");
}

void test_performance() {
  RouteRequest request;
  request.environment_ref = "performance";
  request.map.width = 30;
  request.map.height = 20;
  request.max_time = 100;
  request.amrs = {
      make_amr("A1", GridPosition{0, 0}, 90),
      make_amr("A2", GridPosition{0, 5}, 90),
      make_amr("A3", GridPosition{0, 10}, 90),
      make_amr("A4", GridPosition{0, 15}, 90),
  };
  request.orders = {
      make_order("O1", "P1", "D1", 5),
      make_order("O2", "P2", "D2", 4),
      make_order("O3", "P3", "D3", 3),
      make_order("O4", "P4", "D4", 2),
  };
  request.locations = {
      Location{"P1", GridPosition{3, 0}}, Location{"D1", GridPosition{20, 0}},
      Location{"P2", GridPosition{3, 5}}, Location{"D2", GridPosition{20, 5}},
      Location{"P3", GridPosition{3, 10}}, Location{"D3", GridPosition{20, 10}},
      Location{"P4", GridPosition{3, 15}}, Location{"D4", GridPosition{20, 15}},
  };
  request.assignments = {
      RouteAssignment{"A1", "O1"}, RouteAssignment{"A2", "O2"},
      RouteAssignment{"A3", "O3"}, RouteAssignment{"A4", "O4"},
  };
  const auto started = std::chrono::steady_clock::now();
  const auto result = amr::planner::plan_routes_astar(request);
  const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::steady_clock::now() - started);
  expect(result.status == "complete" && result.planned_count == 4U,
         "performance workload should remain feasible");
  expect(elapsed.count() < 2000,
         "30x20 four-AMR A* workload exceeded the 2 second performance budget");
  std::cout << "performance_ms=" << elapsed.count()
            << " expanded_states=" << result.total_expanded_states << '\n';
}

void test_json_contract() {
  bool rejected = false;
  try {
    amr::planner::json::Value::Object invalid{{"unexpected", amr::planner::json::Value(true)}};
    (void)amr::planner::route_json::request_from_value(
        amr::planner::json::Value(std::move(invalid)));
  } catch (const amr::planner::json::ParseError&) {
    rejected = true;
  }
  expect(rejected, "route JSON boundary must reject unknown/missing fields");
}

void run_case(const std::string& name) {
  if (name == "obstacles") return test_obstacles();
  if (name == "boundary") return test_boundary();
  if (name == "forbidden_edges") return test_forbidden_edges();
  if (name == "one_way_edges") return test_one_way_edges();
  if (name == "wait") return test_wait_and_vertex_reservation();
  if (name == "vertex_conflict") return test_vertex_conflict_direct();
  if (name == "swap_edge_conflict") return test_swap_edge_conflict();
  if (name == "no_solution") return test_no_solution();
  if (name == "dijkstra_baseline") return test_dijkstra_baseline();
  if (name == "reproducibility") return test_reproducibility();
  if (name == "performance") return test_performance();
  if (name == "json_contract") return test_json_contract();
  throw TestFailure("unknown test case: " + name);
}

}  // namespace

int main(int argc, char* argv[]) {
  if (argc != 3 || std::string(argv[1]) != "--case") {
    std::cerr << "usage: route_planner_tests --case <name>\n";
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
