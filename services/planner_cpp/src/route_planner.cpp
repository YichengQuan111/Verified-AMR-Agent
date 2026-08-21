#include "route_planner/route_planner.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <functional>
#include <limits>
#include <map>
#include <queue>
#include <set>
#include <sstream>
#include <tuple>
#include <utility>

namespace amr::planner {
namespace {

constexpr double kEpsilon = 1.0e-9;
constexpr std::size_t kMaxStateCount = 8'000'000U;

using CellTuple = std::pair<int, int>;
using EdgeTuple = std::tuple<int, int, int, int>;

struct SearchState {
  int x{};
  int y{};
  int heading{};
  int time{};
};

struct Candidate {
  SearchState state;
  RouteAction action{RouteAction::kWait};
  double cost{};
};

struct SearchResult {
  bool found{false};
  std::string reason_code{"no_path_within_horizon"};
  std::string reason{"在 max_time 内没有满足障碍和时空预约约束的路径"};
  double cost{};
  std::size_t expanded_states{};
  std::vector<RouteStep> path;
};

struct WorkItem {
  RouteAssignment assignment;
  TransportOrder order;
  AMRState amr;
};

struct NormalizedRequest {
  RouteRequest request;
  std::map<std::string, AMRState> amrs;
  std::map<std::string, TransportOrder> orders;
  std::map<std::string, GridPosition> locations;
  std::set<CellTuple> blocked_cells;
  std::set<EdgeTuple> blocked_edges;
  std::set<EdgeTuple> one_way_edges;
  std::set<std::string> completed_order_ids;
  std::vector<WorkItem> work_items;
  bool duplicate_initial_cell{false};
};

struct QueueEntry {
  double priority{};
  double g_cost{};
  std::size_t sequence{};
  std::size_t index{};
};

struct QueueEntryGreater {
  bool operator()(const QueueEntry& left, const QueueEntry& right) const noexcept {
    if (left.priority != right.priority) return left.priority > right.priority;
    if (left.g_cost != right.g_cost) return left.g_cost > right.g_cost;
    return left.sequence > right.sequence;
  }
};

std::string cell_text(const GridPosition& cell) {
  return "(" + std::to_string(cell.x) + "," + std::to_string(cell.y) + ")";
}

bool same_cell(const GridPosition& left, const GridPosition& right) noexcept {
  return left.x == right.x && left.y == right.y;
}

bool in_bounds(const RouteMap& map, const GridPosition& cell) noexcept {
  return cell.x >= 0 && cell.x < map.width && cell.y >= 0 && cell.y < map.height;
}

bool finite_non_negative(double value) noexcept {
  return std::isfinite(value) && value >= 0.0;
}

int heading_index(int heading) {
  switch (heading) {
    case 0: return 0;
    case 90: return 1;
    case 180: return 2;
    case 270: return 3;
    default: throw RouteError("invalid_heading", "heading 必须是 0、90、180 或 270");
  }
}

int left_heading(int heading) noexcept { return (heading + 270) % 360; }
int right_heading(int heading) noexcept { return (heading + 90) % 360; }

GridPosition forward_cell(const SearchState& state) noexcept {
  GridPosition next{state.x, state.y};
  switch (state.heading) {
    case 0: --next.y; break;
    case 90: ++next.x; break;
    case 180: ++next.y; break;
    case 270: --next.x; break;
    default: break;
  }
  return next;
}

EdgeTuple edge_tuple(const GridPosition& from, const GridPosition& to) noexcept {
  return std::make_tuple(from.x, from.y, to.x, to.y);
}

CellTuple cell_tuple(const GridPosition& cell) noexcept {
  return std::make_pair(cell.x, cell.y);
}

void require_non_empty(const std::string& value,
                       const std::string& field,
                       const std::string& code = "invalid_request") {
  if (value.empty()) throw RouteError(code, field + " 不能为空");
}

void validate_position(const RouteMap& map,
                       const GridPosition& cell,
                       const std::string& field) {
  if (!in_bounds(map, cell)) {
    throw RouteError("position_out_of_bounds",
                     field + " 超出地图边界: " + cell_text(cell));
  }
}

void validate_edges(const RouteMap& map,
                    const std::vector<RouteEdge>& edges,
                    const std::string& field,
                    std::set<EdgeTuple>& destination) {
  for (const auto& edge : edges) {
    validate_position(map, edge.from, field + ".from");
    validate_position(map, edge.to, field + ".to");
    const int manhattan = std::abs(edge.from.x - edge.to.x) +
                          std::abs(edge.from.y - edge.to.y);
    if (manhattan != 1) {
      throw RouteError("invalid_edge", field + " 必须连接相邻栅格");
    }
    if (!destination.insert(edge_tuple(edge.from, edge.to)).second) {
      throw RouteError("duplicate_edge", field + " 不能包含重复边");
    }
  }
}

void validate_dependencies(const NormalizedRequest& normalized) {
  std::map<std::string, int> indegree;
  std::map<std::string, std::vector<std::string>> successors;
  for (const auto& entry : normalized.orders) indegree.emplace(entry.first, 0);

  for (const auto& entry : normalized.orders) {
    const auto& order = entry.second;
    std::set<std::string> dependencies;
    for (const auto& dependency : order.dependencies) {
      if (!dependencies.insert(dependency).second) {
        throw RouteError("invalid_order_dependencies",
                         "订单依赖不能重复: " + order.order_id);
      }
      if (normalized.completed_order_ids.count(dependency) != 0U) continue;
      if (normalized.orders.count(dependency) == 0U) {
        throw RouteError("unknown_order_dependency",
                         "订单依赖不存在且未完成: " + dependency);
      }
      ++indegree[order.order_id];
      successors[dependency].push_back(order.order_id);
    }
  }

  std::set<std::string> ready;
  for (const auto& entry : indegree) {
    if (entry.second == 0) ready.insert(entry.first);
  }
  std::size_t processed = 0;
  while (!ready.empty()) {
    const std::string current = *ready.begin();
    ready.erase(ready.begin());
    ++processed;
    auto next_it = successors.find(current);
    if (next_it == successors.end()) continue;
    std::sort(next_it->second.begin(), next_it->second.end());
    for (const auto& successor : next_it->second) {
      if (--indegree[successor] == 0) ready.insert(successor);
    }
  }
  if (processed != normalized.orders.size()) {
    throw RouteError("order_dependency_cycle", "订单依赖图包含循环");
  }
}

NormalizedRequest normalize_request(const RouteRequest& request) {
  NormalizedRequest normalized;
  normalized.request = request;
  require_non_empty(request.environment_ref, "environment_ref");
  if (request.map.width <= 0 || request.map.height <= 0 || request.map.width > 100 ||
      request.map.height > 100) {
    throw RouteError("invalid_map", "地图尺寸必须在 1..100 范围内");
  }
  if (request.start_time < 0 || request.max_time < request.start_time) {
    throw RouteError("invalid_time_horizon", "start_time/max_time 时间范围非法");
  }
  if (request.max_time > 2000) {
    throw RouteError("planning_horizon_too_large", "max_time 不能超过 2000");
  }
  const std::size_t state_count = static_cast<std::size_t>(request.max_time + 1) *
                                  static_cast<std::size_t>(request.map.width) *
                                  static_cast<std::size_t>(request.map.height) * 4U;
  if (state_count > kMaxStateCount) {
    throw RouteError("planning_state_space_too_large", "时间扩展状态空间超过安全上限");
  }
  if (!std::isfinite(request.costs.move_cost) || request.costs.move_cost <= 0.0 ||
      !finite_non_negative(request.costs.turn_cost) ||
      !finite_non_negative(request.costs.wait_cost)) {
    throw RouteError("invalid_route_costs", "移动代价必须为正，转向/等待代价必须为有限非负数");
  }

  for (const auto& cell : request.map.blocked_cells) {
    validate_position(request.map, cell, "blocked_cells");
    if (!normalized.blocked_cells.insert(cell_tuple(cell)).second) {
      throw RouteError("duplicate_blocked_cell", "blocked_cells 不能包含重复坐标");
    }
  }
  validate_edges(request.map, request.map.blocked_edges, "blocked_edges",
                 normalized.blocked_edges);
  validate_edges(request.map, request.map.one_way_edges, "one_way_edges",
                 normalized.one_way_edges);

  for (const auto& amr : request.amrs) {
    require_non_empty(amr.amr_id, "amr_id");
    validate_position(request.map, amr.position, "AMR position");
    heading_index(amr.heading);
    if (!normalized.amrs.emplace(amr.amr_id, amr).second) {
      throw RouteError("duplicate_amr_id", "amr_id 不能重复: " + amr.amr_id);
    }
  }

  for (const auto& order : request.orders) {
    require_non_empty(order.order_id, "order_id");
    require_non_empty(order.pickup, "pickup");
    require_non_empty(order.dropoff, "dropoff");
    if (order.pickup == order.dropoff) {
      throw RouteError("invalid_order", "pickup 与 dropoff 不能相同: " + order.order_id);
    }
    if (order.priority < 1 || order.priority > 5 || order.release_time < 0 ||
        order.deadline <= order.release_time) {
      throw RouteError("invalid_order_time_or_priority", "订单时间窗或优先级非法: " + order.order_id);
    }
    if (!normalized.orders.emplace(order.order_id, order).second) {
      throw RouteError("duplicate_order_id", "order_id 不能重复: " + order.order_id);
    }
  }

  for (const auto& location : request.locations) {
    require_non_empty(location.location_id, "location_id");
    validate_position(request.map, location.position, "location position");
    if (!normalized.locations.emplace(location.location_id, location.position).second) {
      throw RouteError("duplicate_location_id", "location_id 不能重复: " + location.location_id);
    }
  }

  for (const auto& completed : request.completed_order_ids) {
    require_non_empty(completed, "completed_order_id");
    if (!normalized.completed_order_ids.insert(completed).second) {
      throw RouteError("duplicate_completed_order_id", "completed_order_ids 不能重复");
    }
  }

  std::set<std::string> assigned_amrs;
  std::set<std::string> assigned_orders;
  for (const auto& assignment : request.assignments) {
    require_non_empty(assignment.amr_id, "assignment.amr_id");
    require_non_empty(assignment.order_id, "assignment.order_id");
    const auto amr_it = normalized.amrs.find(assignment.amr_id);
    if (amr_it == normalized.amrs.end()) {
      throw RouteError("unknown_assignment_amr", "assignment 引用了未知 AMR: " + assignment.amr_id);
    }
    const auto order_it = normalized.orders.find(assignment.order_id);
    if (order_it == normalized.orders.end()) {
      throw RouteError("unknown_assignment_order", "assignment 引用了未知订单: " + assignment.order_id);
    }
    if (!assigned_amrs.insert(assignment.amr_id).second) {
      throw RouteError("duplicate_assignment_amr", "一台 AMR 在同一滚动周期最多分配一个订单");
    }
    if (!assigned_orders.insert(assignment.order_id).second) {
      throw RouteError("duplicate_assignment_order", "一个订单不能重复规划");
    }
  }

  validate_dependencies(normalized);

  std::map<CellTuple, std::string> initial_cells;
  for (const auto& entry : normalized.amrs) {
    const CellTuple key = cell_tuple(entry.second.position);
    if (!initial_cells.emplace(key, entry.first).second) {
      normalized.duplicate_initial_cell = true;
    }
  }

  for (const auto& assignment : request.assignments) {
    normalized.work_items.push_back(WorkItem{
        assignment,
        normalized.orders.at(assignment.order_id),
        normalized.amrs.at(assignment.amr_id),
    });
  }
  std::sort(normalized.work_items.begin(), normalized.work_items.end(),
            [](const WorkItem& left, const WorkItem& right) {
              if (left.order.priority != right.order.priority) {
                return left.order.priority > right.order.priority;
              }
              if (left.order.release_time != right.order.release_time) {
                return left.order.release_time < right.order.release_time;
              }
              if (left.order.order_id != right.order.order_id) {
                return left.order.order_id < right.order.order_id;
              }
              return left.amr.amr_id < right.amr.amr_id;
            });
  return normalized;
}

bool cell_blocked(const NormalizedRequest& normalized, const GridPosition& cell) {
  return normalized.blocked_cells.count(cell_tuple(cell)) != 0U;
}

bool movement_allowed(const NormalizedRequest& normalized,
                      const GridPosition& from,
                      const GridPosition& to) {
  if (!in_bounds(normalized.request.map, to) || cell_blocked(normalized, to)) return false;
  const EdgeTuple forward = edge_tuple(from, to);
  if (normalized.blocked_edges.count(forward) != 0U) return false;
  const EdgeTuple reverse = edge_tuple(to, from);
  // one_way_edges 是受限无向边中的允许方向；若只登记反向边，则正向通行被拒绝。
  if (normalized.one_way_edges.count(reverse) != 0U &&
      normalized.one_way_edges.count(forward) == 0U) {
    return false;
  }
  return true;
}

std::size_t state_index(const RouteMap& map, const SearchState& state) {
  const std::size_t cell_count = static_cast<std::size_t>(map.width) *
                                 static_cast<std::size_t>(map.height);
  const std::size_t cell_offset = static_cast<std::size_t>(state.time) * cell_count +
                                  static_cast<std::size_t>(state.y) *
                                      static_cast<std::size_t>(map.width) +
                                  static_cast<std::size_t>(state.x);
  return cell_offset * 4U + static_cast<std::size_t>(heading_index(state.heading));
}

SearchState decode_state(const RouteMap& map, std::size_t index) {
  const std::size_t heading = index % 4U;
  const std::size_t cell_offset = index / 4U;
  const std::size_t cell_count = static_cast<std::size_t>(map.width) *
                                 static_cast<std::size_t>(map.height);
  const std::size_t cell = cell_offset % cell_count;
  return SearchState{
      static_cast<int>(cell % static_cast<std::size_t>(map.width)),
      static_cast<int>(cell / static_cast<std::size_t>(map.width)),
      static_cast<int>(heading * 90U),
      static_cast<int>(cell_offset / cell_count),
  };
}

void append_candidate(const NormalizedRequest& normalized,
                      const ReservationTable& reservations,
                      const SearchState& current,
                      const SearchState& next,
                      RouteAction action,
                      double cost,
                      std::array<Candidate, 4>& output,
                      std::size_t& count) {
  if (count >= output.size() || next.time > normalized.request.max_time) return;
  const GridPosition from{current.x, current.y};
  const GridPosition to{next.x, next.y};
  if (!reservations.can_transition(from, to, current.time)) return;
  output[count++] = Candidate{next, action, cost};
}

std::array<Candidate, 4> candidates_for(const NormalizedRequest& normalized,
                                        const ReservationTable& reservations,
                                        const SearchState& current,
                                        int earliest_goal_time,
                                        std::size_t& count) {
  std::array<Candidate, 4> output{};
  count = 0;
  if (current.time >= normalized.request.max_time) return output;

  // release_time 之前只能等待，避免规划器提前占用资源并违反订单时间窗。
  if (current.time < earliest_goal_time) {
    append_candidate(
        normalized,
        reservations,
        current,
        SearchState{current.x, current.y, current.heading, current.time + 1},
        RouteAction::kWait,
        normalized.request.costs.wait_cost,
        output,
        count);
    return output;
  }

  const GridPosition forward = forward_cell(current);
  if (movement_allowed(normalized, GridPosition{current.x, current.y}, forward)) {
    append_candidate(
        normalized,
        reservations,
        current,
        SearchState{forward.x, forward.y, current.heading, current.time + 1},
        RouteAction::kMove,
        normalized.request.costs.move_cost,
        output,
        count);
  }
  append_candidate(
      normalized,
      reservations,
      current,
      SearchState{current.x, current.y, left_heading(current.heading), current.time + 1},
      RouteAction::kTurnLeft,
      normalized.request.costs.turn_cost,
      output,
      count);
  append_candidate(
      normalized,
      reservations,
      current,
      SearchState{current.x, current.y, right_heading(current.heading), current.time + 1},
      RouteAction::kTurnRight,
      normalized.request.costs.turn_cost,
      output,
      count);
  append_candidate(
      normalized,
      reservations,
      current,
      SearchState{current.x, current.y, current.heading, current.time + 1},
      RouteAction::kWait,
      normalized.request.costs.wait_cost,
      output,
      count);
  return output;
}

double manhattan(const GridPosition& left, const GridPosition& right) noexcept {
  return static_cast<double>(std::abs(left.x - right.x) + std::abs(left.y - right.y));
}

SearchResult reconstruct_search(const RouteMap& map,
                                const std::vector<double>& best_g,
                                const std::vector<int>& parents,
                                const std::vector<RouteAction>& actions,
                                std::size_t goal_index,
                                std::size_t expanded_states) {
  SearchResult result;
  result.found = true;
  result.cost = best_g[goal_index];
  result.expanded_states = expanded_states;
  std::size_t current = goal_index;
  while (true) {
    const SearchState state = decode_state(map, current);
    result.path.push_back(RouteStep{
        GridPosition{state.x, state.y},
        state.heading,
        state.time,
        actions[current],
        best_g[current],
    });
    const int parent = parents[current];
    if (parent < 0) break;
    current = static_cast<std::size_t>(parent);
  }
  std::reverse(result.path.begin(), result.path.end());
  return result;
}

// A* 生产搜索：priority=g+曼哈顿启发式；启发式不估计转向/等待，因而不会
// 高估真实代价。开放表和邻居顺序固定，等价代价保留首次到达以保证复现。
SearchResult search_astar(const NormalizedRequest& normalized,
                          const SearchState& start,
                          const GridPosition& goal,
                          int earliest_goal_time,
                          const ReservationTable& reservations) {
  const RouteMap& map = normalized.request.map;
  const std::size_t state_count = static_cast<std::size_t>(map.width) *
                                  static_cast<std::size_t>(map.height) * 4U *
                                  static_cast<std::size_t>(normalized.request.max_time + 1);
  const double infinity = std::numeric_limits<double>::infinity();
  std::vector<double> best_g(state_count, infinity);
  std::vector<int> parents(state_count, -2);
  std::vector<RouteAction> actions(state_count, RouteAction::kStart);
  std::priority_queue<QueueEntry, std::vector<QueueEntry>, QueueEntryGreater> open;

  const std::size_t start_index = state_index(map, start);
  if (reservations.is_cell_reserved(GridPosition{start.x, start.y}, start.time)) {
    return SearchResult{false, "start_cell_reserved", "起点在起始时刻已被时空预约占用"};
  }
  best_g[start_index] = 0.0;
  parents[start_index] = -1;
  actions[start_index] = RouteAction::kStart;
  std::size_t sequence = 0;
  open.push(QueueEntry{
      manhattan(GridPosition{start.x, start.y}, goal) * normalized.request.costs.move_cost,
      0.0,
      sequence++,
      start_index,
  });

  std::size_t expanded_states = 0;
  while (!open.empty()) {
    const QueueEntry current_entry = open.top();
    open.pop();
    if (current_entry.g_cost > best_g[current_entry.index] + kEpsilon) continue;
    const SearchState current = decode_state(map, current_entry.index);
    ++expanded_states;
    if (same_cell(GridPosition{current.x, current.y}, goal) &&
        current.time >= earliest_goal_time) {
      return reconstruct_search(map, best_g, parents, actions, current_entry.index,
                                expanded_states);
    }

    std::size_t candidate_count = 0;
    const auto candidates = candidates_for(normalized, reservations, current,
                                           earliest_goal_time, candidate_count);
    for (std::size_t index = 0; index < candidate_count; ++index) {
      const auto& candidate = candidates[index];
      const std::size_t next_index = state_index(map, candidate.state);
      const double next_g = best_g[current_entry.index] + candidate.cost;
      if (next_g + kEpsilon >= best_g[next_index]) continue;
      best_g[next_index] = next_g;
      parents[next_index] = static_cast<int>(current_entry.index);
      actions[next_index] = candidate.action;
      const double heuristic =
          manhattan(GridPosition{candidate.state.x, candidate.state.y}, goal) *
          normalized.request.costs.move_cost;
      open.push(QueueEntry{next_g + heuristic, next_g, sequence++, next_index});
    }
  }
  SearchResult result;
  result.expanded_states = expanded_states;
  return result;
}

// Dijkstra 正确性基线：使用独立的 g 值最短路开放表，h 恒为 0；不调用
// search_astar，也不共享其 priority/启发式逻辑，避免基线“验证自己”。
SearchResult search_dijkstra(const NormalizedRequest& normalized,
                             const SearchState& start,
                             const GridPosition& goal,
                             int earliest_goal_time,
                             const ReservationTable& reservations) {
  const RouteMap& map = normalized.request.map;
  const std::size_t state_count = static_cast<std::size_t>(map.width) *
                                  static_cast<std::size_t>(map.height) * 4U *
                                  static_cast<std::size_t>(normalized.request.max_time + 1);
  const double infinity = std::numeric_limits<double>::infinity();
  std::vector<double> distance(state_count, infinity);
  std::vector<int> parents(state_count, -2);
  std::vector<RouteAction> actions(state_count, RouteAction::kStart);
  std::priority_queue<QueueEntry, std::vector<QueueEntry>, QueueEntryGreater> frontier;

  const std::size_t start_index = state_index(map, start);
  if (reservations.is_cell_reserved(GridPosition{start.x, start.y}, start.time)) {
    return SearchResult{false, "start_cell_reserved", "起点在起始时刻已被时空预约占用"};
  }
  distance[start_index] = 0.0;
  parents[start_index] = -1;
  actions[start_index] = RouteAction::kStart;
  std::size_t sequence = 0;
  frontier.push(QueueEntry{0.0, 0.0, sequence++, start_index});

  std::size_t expanded_states = 0;
  while (!frontier.empty()) {
    const QueueEntry current_entry = frontier.top();
    frontier.pop();
    if (current_entry.g_cost > distance[current_entry.index] + kEpsilon) continue;
    const SearchState current = decode_state(map, current_entry.index);
    ++expanded_states;
    if (same_cell(GridPosition{current.x, current.y}, goal) &&
        current.time >= earliest_goal_time) {
      return reconstruct_search(map, distance, parents, actions, current_entry.index,
                                expanded_states);
    }

    std::size_t candidate_count = 0;
    const auto candidates = candidates_for(normalized, reservations, current,
                                           earliest_goal_time, candidate_count);
    for (std::size_t index = 0; index < candidate_count; ++index) {
      const auto& candidate = candidates[index];
      const std::size_t next_index = state_index(map, candidate.state);
      const double next_distance = distance[current_entry.index] + candidate.cost;
      if (next_distance + kEpsilon >= distance[next_index]) continue;
      distance[next_index] = next_distance;
      parents[next_index] = static_cast<int>(current_entry.index);
      actions[next_index] = candidate.action;
      frontier.push(QueueEntry{next_distance, next_distance, sequence++, next_index});
    }
  }
  SearchResult result;
  result.expanded_states = expanded_states;
  return result;
}

using SearchFunction = SearchResult (*)(const NormalizedRequest&,
                                        const SearchState&,
                                        const GridPosition&,
                                        int,
                                        const ReservationTable&);

PlannedRoute failure_route(const WorkItem& item,
                           const std::string& reason_code,
                           const std::string& reason) {
  PlannedRoute route;
  route.amr_id = item.amr.amr_id;
  route.order_id = item.order.order_id;
  route.priority = item.order.priority;
  route.status = "infeasible";
  route.reason_code = reason_code;
  route.reason = reason;
  return route;
}

PlannedRoute plan_one_route(const NormalizedRequest& normalized,
                            const WorkItem& item,
                            const ReservationTable& reservations,
                            SearchFunction search) {
  const auto pickup_it = normalized.locations.find(item.order.pickup);
  if (pickup_it == normalized.locations.end()) {
    return failure_route(item, "pickup_location_missing", "pickup 工位未在位置快照中找到");
  }
  const auto dropoff_it = normalized.locations.find(item.order.dropoff);
  if (dropoff_it == normalized.locations.end()) {
    return failure_route(item, "dropoff_location_missing", "dropoff 工位未在位置快照中找到");
  }

  if (normalized.completed_order_ids.count(item.order.order_id) != 0U) {
    return failure_route(item, "order_already_completed", "订单已在 completed_order_ids 中，不应再次规划");
  }
  for (const auto& dependency : item.order.dependencies) {
    if (normalized.completed_order_ids.count(dependency) == 0U) {
      return failure_route(item, "order_dependency_pending", "订单前置依赖尚未完成: " + dependency);
    }
  }

  const GridPosition start_cell = item.amr.position;
  const GridPosition pickup = pickup_it->second;
  const GridPosition dropoff = dropoff_it->second;
  if (cell_blocked(normalized, start_cell)) {
    return failure_route(item, "start_cell_blocked", "AMR 起点位于障碍或当前禁行区");
  }
  if (cell_blocked(normalized, pickup)) {
    return failure_route(item, "pickup_cell_blocked", "pickup 位于障碍或当前禁行区");
  }
  if (cell_blocked(normalized, dropoff)) {
    return failure_route(item, "dropoff_cell_blocked", "dropoff 位于障碍或当前禁行区");
  }

  const SearchState start{
      start_cell.x,
      start_cell.y,
      item.amr.heading,
      normalized.request.start_time,
  };
  const SearchResult to_pickup = search(
      normalized, start, pickup, std::max(normalized.request.start_time, item.order.release_time),
      reservations);
  if (!to_pickup.found) {
    return failure_route(item, "no_safe_path_to_pickup", to_pickup.reason);
  }

  const auto& pickup_state = to_pickup.path.back();
  const SearchState pickup_start{
      pickup_state.position.x,
      pickup_state.position.y,
      pickup_state.heading,
      pickup_state.time,
  };
  const SearchResult to_dropoff = search(normalized, pickup_start, dropoff,
                                         pickup_state.time, reservations);
  if (!to_dropoff.found) {
    return failure_route(item, "no_safe_path_to_dropoff", to_dropoff.reason);
  }

  PlannedRoute route;
  route.amr_id = item.amr.amr_id;
  route.order_id = item.order.order_id;
  route.priority = item.order.priority;
  route.status = "planned";
  route.pickup_time = pickup_state.time;
  route.dropoff_time = to_dropoff.path.back().time;
  route.total_cost = to_pickup.cost + to_dropoff.cost;
  route.expanded_states = to_pickup.expanded_states + to_dropoff.expanded_states;
  route.path = to_pickup.path;
  for (std::size_t index = 1; index < to_dropoff.path.size(); ++index) {
    RouteStep step = to_dropoff.path[index];
    step.g_cost += to_pickup.cost;
    route.path.push_back(step);
  }
  return route;
}

RoutePlanResult plan_with_search(const RouteRequest& request,
                                 RouteAlgorithm algorithm,
                                 SearchFunction search) {
  const NormalizedRequest normalized = normalize_request(request);
  RoutePlanResult result;
  result.algorithm = algorithm == RouteAlgorithm::kAStar ? "astar" : "dijkstra";
  result.status = "complete";
  result.routes.reserve(normalized.work_items.size());

  if (normalized.duplicate_initial_cell) {
    result.status = "infeasible";
    for (const auto& item : normalized.work_items) {
      result.routes.push_back(failure_route(
          item, "duplicate_initial_cell", "两台或多台 AMR 在同一时刻占用同一初始栅格"));
    }
    return result;
  }

  ReservationTable reservations(request.max_time);
  std::set<std::string> assigned_amr_ids;
  for (const auto& item : normalized.work_items) {
    assigned_amr_ids.insert(item.amr.amr_id);
  }
  for (const auto& entry : normalized.amrs) {
    if (assigned_amr_ids.count(entry.first) != 0U) continue;
    // 本轮未分配任务不等于车辆从物理世界消失。它会在初始栅格保持占用到
    // max_time；否则已分配路线可能穿过 idle/offline AMR，直到 P0-10 才被拒绝，
    // 造成 P0-09 自称 complete 但下游必然 invalid 的接口语义漂移。
    const auto& amr = entry.second;
    reservations.reserve_path(
        {RouteStep{amr.position, amr.heading, request.start_time,
                   RouteAction::kStart, 0.0}},
        request.max_time);
  }
  for (const auto& item : normalized.work_items) {
    PlannedRoute route = plan_one_route(normalized, item, reservations, search);
    result.total_expanded_states += route.expanded_states;
    if (route.status == "planned") {
      reservations.reserve_path(route.path, request.max_time);
      ++result.planned_count;
      result.total_cost += route.total_cost;
    } else {
      result.status = "infeasible";
    }
    result.routes.push_back(std::move(route));
  }
  result.cell_reservation_count = reservations.cell_reservation_count();
  result.edge_reservation_count = reservations.edge_reservation_count();
  return result;
}

}  // namespace

RouteError::RouteError(std::string code, std::string message)
    : std::runtime_error(std::move(message)), code_(std::move(code)) {}

bool ReservationTable::CellKey::operator<(const CellKey& other) const noexcept {
  return std::tie(x, y, time) < std::tie(other.x, other.y, other.time);
}

bool ReservationTable::EdgeKey::operator<(const EdgeKey& other) const noexcept {
  return std::tie(from_x, from_y, to_x, to_y, time) <
         std::tie(other.from_x, other.from_y, other.to_x, other.to_y, other.time);
}

ReservationTable::ReservationTable(int max_time) : max_time_(max_time) {
  if (max_time < 0) throw RouteError("invalid_time_horizon", "预约表 max_time 不能为负数");
}

bool ReservationTable::is_cell_reserved(const GridPosition& cell, int time) const noexcept {
  if (time < 0 || time > max_time_) return false;
  return cells_.find(CellKey{cell.x, cell.y, time}) != cells_.end();
}

bool ReservationTable::is_edge_reserved(const GridPosition& from,
                                        const GridPosition& to,
                                        int departure_time) const noexcept {
  if (departure_time < 0 || departure_time >= max_time_) return false;
  return edges_.find(EdgeKey{from.x, from.y, to.x, to.y, departure_time}) != edges_.end();
}

bool ReservationTable::can_transition(const GridPosition& from,
                                      const GridPosition& to,
                                      int departure_time) const noexcept {
  if (departure_time < 0 || departure_time >= max_time_) return false;
  if (is_cell_reserved(to, departure_time + 1)) return false;
  if (same_cell(from, to)) return true;
  // 正向重复边由目标 cell 预约通常已经拒绝，但显式检查能让 API 对调用方
  // 直接表达“边已占用”；反向检查则是交换边冲突的关键安全门禁。
  return !is_edge_reserved(from, to, departure_time) &&
         !is_edge_reserved(to, from, departure_time);
}

void ReservationTable::reserve_path(const std::vector<RouteStep>& path, int hold_until) {
  if (path.empty()) throw RouteError("invalid_path", "不能预约空路径");
  if (hold_until < path.back().time || hold_until > max_time_) {
    throw RouteError("invalid_path", "路径保持时间超出预约表时间范围");
  }
  for (std::size_t index = 0; index < path.size(); ++index) {
    const auto& step = path[index];
    if (step.time < 0 || step.time > max_time_) {
      throw RouteError("invalid_path", "路径时间超出预约表时间范围");
    }
    if (index > 0 && step.time != path[index - 1].time + 1) {
      throw RouteError("invalid_path", "路径时间必须逐步递增且不能跳时刻");
    }
    cells_.insert(CellKey{step.position.x, step.position.y, step.time});
    if (index > 0 && !same_cell(path[index - 1].position, step.position)) {
      edges_.insert(EdgeKey{
          path[index - 1].position.x,
          path[index - 1].position.y,
          step.position.x,
          step.position.y,
          path[index - 1].time,
      });
    }
  }
  const auto& final_step = path.back();
  for (int time = final_step.time + 1; time <= hold_until; ++time) {
    cells_.insert(CellKey{final_step.position.x, final_step.position.y, time});
  }
}

const char* route_action_name(RouteAction action) noexcept {
  switch (action) {
    case RouteAction::kStart: return "start";
    case RouteAction::kMove: return "move";
    case RouteAction::kTurnLeft: return "turn_left";
    case RouteAction::kTurnRight: return "turn_right";
    case RouteAction::kWait: return "wait";
  }
  return "unknown";
}

RoutePlanResult plan_routes_astar(const RouteRequest& request) {
  return plan_with_search(request, RouteAlgorithm::kAStar, search_astar);
}

RoutePlanResult plan_routes_dijkstra(const RouteRequest& request) {
  return plan_with_search(request, RouteAlgorithm::kDijkstra, search_dijkstra);
}

RoutePlanResult plan_multi_amr_routes(const RouteRequest& request,
                                      RouteAlgorithm algorithm) {
  return algorithm == RouteAlgorithm::kAStar ? plan_routes_astar(request)
                                             : plan_routes_dijkstra(request);
}

}  // namespace amr::planner
