#include "fleet_plan_validator/fleet_plan_validator.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <map>
#include <set>
#include <string_view>
#include <tuple>
#include <utility>

namespace amr::planner::validator {
namespace {

constexpr double kEpsilon = 1.0e-9;
constexpr std::size_t kMaxValidationErrors = 4096U;

using CellKey = std::pair<int, int>;
using EdgeKey = std::tuple<int, int, int, int>;

struct DerivedRoute {
  bool usable{false};
  int pickup_index{-1};
  int dropoff_index{-1};
  int move_count{0};
};

struct NormalizedPlan {
  RouteMap map;
  std::map<std::string, AMRState> amrs;
  std::map<std::string, TransportOrder> orders;
  std::map<std::string, GridPosition> locations;
  std::map<std::string, std::size_t> route_by_amr;
  std::map<std::string, std::size_t> route_by_order;
  std::set<CellKey> blocked_cells;
  std::set<EdgeKey> blocked_edges;
  std::set<EdgeKey> one_way_edges;
  std::set<std::string> completed_orders;
  std::vector<DerivedRoute> derived_routes;
};

struct StationEvent {
  std::string station_id;
  std::string order_id;
  std::string amr_id;
  GridPosition coordinate;
  int time{};
};

const ValidationErrorDefinition* find_definition(const std::string& code) noexcept {
  const auto& definitions = error_dictionary();
  const auto it = std::lower_bound(
      definitions.begin(), definitions.end(), code,
      [](const ValidationErrorDefinition& definition, const std::string& value) {
        return definition.code < value;
      });
  return it == definitions.end() || it->code != code ? nullptr : &*it;
}

std::string cell_text(const GridPosition& cell) {
  return "(" + std::to_string(cell.x) + "," + std::to_string(cell.y) + ")";
}

bool same_cell(const GridPosition& left, const GridPosition& right) noexcept {
  return left.x == right.x && left.y == right.y;
}

int manhattan(const GridPosition& left, const GridPosition& right) noexcept {
  // 先提升到 long long 再相减，避免恶意直连调用传入 INT_MIN/INT_MAX 时整数溢出；
  // 超出 int 的距离只会用于“是否相邻/是否低于阈值”判断，饱和到 INT_MAX 足够。
  const auto absolute_difference = [](int first, int second) {
    const long long left_value = first;
    const long long right_value = second;
    return left_value >= right_value ? left_value - right_value : right_value - left_value;
  };
  const long long distance = absolute_difference(left.x, right.x) +
                             absolute_difference(left.y, right.y);
  return distance > static_cast<long long>(std::numeric_limits<int>::max())
             ? std::numeric_limits<int>::max()
             : static_cast<int>(distance);
}

CellKey cell_key(const GridPosition& cell) noexcept {
  return {cell.x, cell.y};
}

EdgeKey edge_key(const GridPosition& from, const GridPosition& to) noexcept {
  return {from.x, from.y, to.x, to.y};
}

bool in_bounds(const RouteMap& map, const GridPosition& cell) noexcept {
  return cell.x >= 0 && cell.x < map.width && cell.y >= 0 && cell.y < map.height;
}

bool finite_non_negative(double value) noexcept {
  return std::isfinite(value) && value >= 0.0;
}

bool valid_heading(int heading) noexcept {
  return heading == 0 || heading == 90 || heading == 180 || heading == 270;
}

int left_heading(int heading) noexcept {
  return valid_heading(heading) ? (heading + 270) % 360 : -1;
}
int right_heading(int heading) noexcept {
  return valid_heading(heading) ? (heading + 90) % 360 : -1;
}

GridPosition forward_cell(const GridPosition& position, int heading) noexcept {
  // 只在 int 边界内推进；越界位置本身稍后会被 route_out_of_bounds 报告，不能在
  // 这里因为构造“下一格”而触发未定义行为或让恶意输入改变验证流程。
  GridPosition next = position;
  switch (heading) {
    case 0:
      if (next.y > std::numeric_limits<int>::min()) --next.y;
      break;
    case 90:
      if (next.x < std::numeric_limits<int>::max()) ++next.x;
      break;
    case 180:
      if (next.y < std::numeric_limits<int>::max()) ++next.y;
      break;
    case 270:
      if (next.x > std::numeric_limits<int>::min()) --next.x;
      break;
    default:
      break;
  }
  return next;
}

bool cell_blocked(const NormalizedPlan& normalized, const GridPosition& cell) {
  return normalized.blocked_cells.count(cell_key(cell)) != 0U;
}

bool movement_allowed(const NormalizedPlan& normalized,
                      const GridPosition& from,
                      const GridPosition& to) {
  if (!in_bounds(normalized.map, to) || cell_blocked(normalized, to)) return false;
  if (normalized.blocked_edges.count(edge_key(from, to)) != 0U) return false;
  // 与 P0-09 保持同一语义：one_way_edges 记录允许方向；若只记录反向边，
  // 当前方向被拒绝。双方都记录时表示该局部边在本快照中允许双向通行。
  const EdgeKey forward = edge_key(from, to);
  const EdgeKey reverse = edge_key(to, from);
  return normalized.one_way_edges.count(reverse) == 0U ||
         normalized.one_way_edges.count(forward) != 0U;
}

void push_error(ValidationResult& result,
                const std::string& code,
                const std::string& message,
                const std::string& order_id = {},
                const std::string& related_order_id = {},
                const std::string& amr_id = {},
                const std::string& related_amr_id = {},
                std::optional<GridPosition> coordinate = std::nullopt,
                std::optional<GridPosition> related_coordinate = std::nullopt,
                std::optional<int> time = std::nullopt,
                std::optional<int> related_time = std::nullopt,
                std::optional<double> observed = std::nullopt,
                std::optional<double> limit = std::nullopt,
                int path_index = -1,
                int related_path_index = -1) {
  // 大型恶意输入不能让错误数组无限膨胀；截断只影响报告体积，不会把计划
  // 改判为合法。正常 P0 车队规模远小于该上限，且排序仍保持跨次稳定。
  if (result.errors.size() >= kMaxValidationErrors) return;
  const auto* definition = find_definition(code);
  ValidationEvidence evidence;
  evidence.code = code;
  evidence.constraint = definition == nullptr ? "unknown" : definition->constraint;
  evidence.message = message;
  evidence.task_id = order_id;
  evidence.related_task_id = related_order_id;
  evidence.order_id = order_id;
  evidence.related_order_id = related_order_id;
  evidence.amr_id = amr_id;
  evidence.related_amr_id = related_amr_id;
  evidence.coordinate = coordinate;
  evidence.related_coordinate = related_coordinate;
  evidence.time = time;
  evidence.related_time = related_time;
  evidence.observed = observed;
  evidence.limit = limit;
  evidence.path_index = path_index;
  evidence.related_path_index = related_path_index;
  result.errors.push_back(std::move(evidence));
}

void push_simple_error(ValidationResult& result,
                       const std::string& code,
                       const std::string& message) {
  push_error(result, code, message);
}

void validate_config(const FleetPlanRequest& request, ValidationResult& result) {
  const auto& config = request.config;
  if (!finite_non_negative(config.maximum_load_kg) || config.maximum_load_kg <= 0.0 ||
      !finite_non_negative(config.energy_per_cell_percent) ||
      !finite_non_negative(config.battery_safety_reserve_percent) ||
      !finite_non_negative(config.new_task_battery_threshold_percent) ||
      !finite_non_negative(config.critical_battery_threshold_percent) ||
      config.battery_safety_reserve_percent > 100.0 ||
      config.new_task_battery_threshold_percent > 100.0 ||
      config.critical_battery_threshold_percent > 100.0 ||
      config.critical_battery_threshold_percent >
          config.new_task_battery_threshold_percent ||
      config.minimum_safety_distance_cells < 0 ||
      config.default_workstation_capacity <= 0) {
    push_simple_error(result, "invalid_config",
                      "Validator 配置必须是有限值，且电量阈值、容量和安全距离满足单调边界");
  }
  if (request.ruleset_version != "p0-10.v1") {
    push_simple_error(result, "invalid_config", "ruleset_version 必须为 p0-10.v1");
  }
}

void validate_map(const FleetPlanRequest& request,
                  NormalizedPlan& normalized,
                  ValidationResult& result) {
  if (request.environment_ref.empty()) {
    push_simple_error(result, "environment_ref_empty", "environment_ref 不能为空");
  }
  if (request.map.width <= 0 || request.map.height <= 0 || request.map.width > 100 ||
      request.map.height > 100) {
    push_simple_error(result, "invalid_map", "地图宽高必须在 1..100 范围内");
    return;
  }
  if (request.start_time < 0 || request.max_time < request.start_time ||
      request.max_time > 2000) {
    push_simple_error(result, "invalid_time_horizon",
                      "start_time/max_time 必须为非负且 max_time 不超过 2000");
  }

  for (const auto& cell : request.map.blocked_cells) {
    if (!in_bounds(request.map, cell)) {
      push_error(result, "route_out_of_bounds", "blocked_cells 坐标超出地图边界", {}, {}, {},
                 {}, cell);
      continue;
    }
    if (!normalized.blocked_cells.insert(cell_key(cell)).second) {
      push_error(result, "duplicate_blocked_cell", "blocked_cells 不能包含重复坐标", {}, {},
                 {}, {}, cell);
    }
  }

  auto validate_edges = [&](const std::vector<RouteEdge>& edges,
                            const std::string& duplicate_code,
                            const std::string& invalid_code,
                            std::set<EdgeKey>& destination) {
    for (const auto& edge : edges) {
      if (!in_bounds(request.map, edge.from) || !in_bounds(request.map, edge.to) ||
          manhattan(edge.from, edge.to) != 1) {
        push_error(result, invalid_code, "地图边必须连接地图内的相邻栅格", {}, {}, {}, {},
                   edge.from, edge.to);
        continue;
      }
      if (!destination.insert(edge_key(edge.from, edge.to)).second) {
        push_error(result, duplicate_code, "地图边不能重复声明", {}, {}, {}, {}, edge.from,
                   edge.to);
      }
    }
  };
  validate_edges(request.map.blocked_edges, "duplicate_blocked_edge", "invalid_blocked_edge",
                 normalized.blocked_edges);
  validate_edges(request.map.one_way_edges, "duplicate_one_way_edge", "invalid_one_way_edge",
                 normalized.one_way_edges);
}

void validate_amrs(const FleetPlanRequest& request,
                   NormalizedPlan& normalized,
                   ValidationResult& result) {
  for (const auto& amr : request.amrs) {
    if (amr.amr_id.empty()) {
      push_error(result, "invalid_config", "AMR ID 不能为空");
      continue;
    }
    if (!normalized.amrs.emplace(amr.amr_id, amr).second) {
      push_error(result, "duplicate_amr_id", "amr_id 不能重复", {}, {}, amr.amr_id);
      continue;
    }
    if (!in_bounds(request.map, amr.position)) {
      push_error(result, "route_out_of_bounds", "AMR 初始坐标超出地图边界", {}, {},
                 amr.amr_id, {}, amr.position);
    } else if (cell_blocked(normalized, amr.position)) {
      push_error(result, "forbidden_zone_occupied", "AMR 初始坐标位于禁行区", {}, {},
                 amr.amr_id, {}, amr.position, {}, request.start_time);
    }
    if (!valid_heading(amr.heading)) {
      push_error(result, "route_heading_invalid", "AMR 初始朝向不在 0/90/180/270 中", {}, {},
                 amr.amr_id, {}, amr.position, {}, request.start_time);
    }
    if (!std::isfinite(amr.battery) || amr.battery < 0.0 || amr.battery > 100.0 ||
        !finite_non_negative(amr.load)) {
      push_error(result, "invalid_config", "AMR 电量或载荷不是合法有限值", {}, {}, amr.amr_id,
                 {}, amr.position, {}, request.start_time);
    }
    if (amr.task_status != AMRTaskStatus::kIdle ||
        amr.health_status != HealthStatus::kHealthy ||
        amr.connection_status != ConnectionStatus::kOnline) {
      push_error(result, "amr_unavailable",
                 "只有 IDLE、HEALTHY、ONLINE 的 AMR 可以执行本计划", {}, {}, amr.amr_id,
                 {}, amr.position, {}, request.start_time);
    }
  }
}

void validate_orders(const FleetPlanRequest& request,
                     NormalizedPlan& normalized,
                     ValidationResult& result) {
  for (const auto& order : request.orders) {
    if (order.order_id.empty()) {
      push_simple_error(result, "invalid_order", "order_id 不能为空");
      continue;
    }
    if (!normalized.orders.emplace(order.order_id, order).second) {
      push_error(result, "duplicate_order_id", "order_id 不能重复", order.order_id);
      continue;
    }
    if (order.pickup.empty() || order.dropoff.empty() || order.pickup == order.dropoff ||
        order.priority < 1 || order.priority > 5 || order.release_time < 0 ||
        order.deadline <= order.release_time) {
      push_error(result, "invalid_order", "订单工位、优先级或时间窗非法", order.order_id);
    }
  }

  for (const auto& location : request.locations) {
    if (location.location_id.empty()) {
      push_simple_error(result, "invalid_config", "location_id 不能为空");
      continue;
    }
    if (!normalized.locations.emplace(location.location_id, location.position).second) {
      push_error(result, "duplicate_location_id", "location_id 不能重复", {}, {}, {}, {},
                 location.position);
    }
    if (!in_bounds(request.map, location.position)) {
      push_error(result, "route_out_of_bounds", "工位坐标超出地图边界", {}, {}, {}, {},
                 location.position);
    } else if (cell_blocked(normalized, location.position)) {
      push_error(result, "forbidden_zone_occupied", "工位位于禁行区", {}, {}, {}, {},
                 location.position);
    }
  }

  for (const auto& completed : request.completed_order_ids) {
    if (completed.empty()) {
      push_error(result, "invalid_completed_order_id", "completed_order_ids 中不能有空 ID");
      continue;
    }
    if (!normalized.completed_orders.insert(completed).second) {
      push_error(result, "duplicate_completed_order_id",
                 "completed_order_ids 不能包含重复 ID", completed);
    }
  }

  // 依赖先检查引用，再以稳定 Kahn 顺序检查环；completed_order_ids 中的外部订单
  // 被视为已满足，不会因为不在当前滚动窗口而误报未知依赖。
  std::map<std::string, int> indegree;
  std::map<std::string, std::vector<std::string>> successors;
  for (const auto& entry : normalized.orders) indegree.emplace(entry.first, 0);
  for (const auto& entry : normalized.orders) {
    const auto& order = entry.second;
    std::set<std::string> seen;
    for (const auto& dependency : order.dependencies) {
      if (!seen.insert(dependency).second) {
        push_error(result, "duplicate_order_dependency", "订单依赖不能重复", order.order_id,
                   dependency);
        continue;
      }
      if (dependency == order.order_id) {
        push_error(result, "order_dependency_cycle", "订单不能依赖自身", order.order_id,
                   dependency);
        continue;
      }
      if (normalized.completed_orders.count(dependency) != 0U) continue;
      if (normalized.orders.count(dependency) == 0U) {
        push_error(result, "unknown_order_dependency", "订单依赖既未完成也不在当前计划", order.order_id,
                   dependency);
        continue;
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
    auto it = successors.find(current);
    if (it == successors.end()) continue;
    std::sort(it->second.begin(), it->second.end());
    for (const auto& successor : it->second) {
      if (--indegree[successor] == 0) ready.insert(successor);
    }
  }
  if (processed != indegree.size()) {
    for (const auto& entry : indegree) {
      if (entry.second > 0) {
        push_error(result, "order_dependency_cycle", "订单依赖图存在循环", entry.first);
      }
    }
  }

  for (const auto& entry : normalized.orders) {
    const auto& order = entry.second;
    const auto pickup = normalized.locations.find(order.pickup);
    const auto dropoff = normalized.locations.find(order.dropoff);
    if (pickup == normalized.locations.end()) {
      push_error(result, "pickup_location_missing", "订单 pickup 工位不在位置快照中",
                 order.order_id);
    }
    if (dropoff == normalized.locations.end()) {
      push_error(result, "dropoff_location_missing", "订单 dropoff 工位不在位置快照中",
                 order.order_id);
    }
  }
}

void validate_completed_order_conflicts(const FleetPlanRequest& request,
                                        const NormalizedPlan& normalized,
                                        ValidationResult& result) {
  for (const auto& completed : normalized.completed_orders) {
    if (normalized.orders.count(completed) == 0U) continue;
    const auto route = normalized.route_by_order.find(completed);
    if (route != normalized.route_by_order.end()) {
      push_error(result, "order_already_completed",
                 "completed_order_ids 中的订单不能再次出现在执行路线", completed, {},
                 request.routes[route->second].amr_id);
    }
  }
}

void validate_route_status_and_payload(const FleetPlanRequest& request,
                                       const FleetPlanRoute& route,
                                       const std::map<std::string, AMRState>& amrs,
                                       const std::map<std::string, TransportOrder>& orders,
                                       ValidationResult& result) {
  if (route.status != "planned") {
    std::string message = "P0-09 路线状态不是 planned";
    if (!route.planner_reason_code.empty()) {
      message += ": " + route.planner_reason_code;
    }
    push_error(result, "route_not_planned", message, route.order_id, {}, route.amr_id);
  }
  if (!std::isfinite(route.payload_kg) || route.payload_kg < 0.0) {
    push_error(result, "route_payload_invalid", "payload_kg 必须是有限非负数", route.order_id,
               {}, route.amr_id);
  }
  const auto amr = amrs.find(route.amr_id);
  const auto order = orders.find(route.order_id);
  if (amr == amrs.end() || order == orders.end()) return;
  if (amr->second.load > request.config.maximum_load_kg + kEpsilon) {
    push_error(result, "load_capacity_exceeded", "AMR 初始载荷已经超过最大载荷", order->first,
               {}, amr->first, {}, amr->second.position, {}, request.start_time,
               std::nullopt, amr->second.load, request.config.maximum_load_kg);
  }
}

void validate_route_path(const FleetPlanRequest& request,
                         NormalizedPlan& normalized,
                         std::size_t route_index,
                         ValidationResult& result) {
  const auto& route = request.routes[route_index];
  auto& derived = normalized.derived_routes[route_index];
  const auto amr_it = normalized.amrs.find(route.amr_id);
  const auto order_it = normalized.orders.find(route.order_id);
  if (amr_it == normalized.amrs.end() || order_it == normalized.orders.end()) return;
  const auto& amr = amr_it->second;
  const auto& order = order_it->second;

  if (route.path.empty()) {
    push_error(result, "route_empty", "planned 路线不能是空路径", order.order_id, {}, amr.amr_id);
    return;
  }
  derived.usable = true;
  const auto pickup_it = normalized.locations.find(order.pickup);
  const auto dropoff_it = normalized.locations.find(order.dropoff);
  if (pickup_it == normalized.locations.end() || dropoff_it == normalized.locations.end()) {
    return;
  }

  const auto& first = route.path.front();
  if (first.time != request.start_time || !same_cell(first.position, amr.position) ||
      first.heading != amr.heading || first.action != RouteAction::kStart) {
    push_error(result, "route_start_mismatch", "路线首状态必须等于 AMR 快照和 start_time",
               order.order_id, {}, amr.amr_id, {}, first.position, {}, first.time,
               request.start_time, std::nullopt, std::nullopt, 0);
  }

  int first_pickup_index = -1;
  int pickup_event_index = -1;
  for (std::size_t index = 0; index < route.path.size(); ++index) {
    const auto& step = route.path[index];
    if (step.time < request.start_time || step.time > request.max_time) {
      push_error(result, "route_time_invalid", "路径时间超出 start_time..max_time 范围",
                 order.order_id, {}, amr.amr_id, {}, step.position, {}, step.time,
                 std::nullopt, std::nullopt, std::nullopt, static_cast<int>(index));
    }
    if (!in_bounds(request.map, step.position)) {
      push_error(result, "route_out_of_bounds", "路径坐标超出地图边界", order.order_id, {},
                 amr.amr_id, {}, step.position, {}, step.time, std::nullopt, std::nullopt,
                 std::nullopt, static_cast<int>(index));
    } else if (cell_blocked(normalized, step.position)) {
      push_error(result, "forbidden_zone_occupied", "路径进入禁行区", order.order_id, {},
                 amr.amr_id, {}, step.position, {}, step.time, std::nullopt, std::nullopt,
                 std::nullopt, static_cast<int>(index));
    }
    if (!valid_heading(step.heading)) {
      push_error(result, "route_heading_invalid", "路径朝向不在 0/90/180/270 中", order.order_id,
                 {}, amr.amr_id, {}, step.position, {}, step.time, std::nullopt,
                 std::nullopt, std::nullopt, static_cast<int>(index));
    }
    if (!std::isfinite(step.g_cost) || step.g_cost < -kEpsilon) {
      push_error(result, "route_cost_invalid", "路径累计代价必须是有限非负数", order.order_id,
                 {}, amr.amr_id, {}, step.position, {}, step.time, std::nullopt,
                 std::nullopt, std::nullopt, static_cast<int>(index));
    }
    if (index > 0) {
      const auto& previous = route.path[index - 1];
      if (step.time != previous.time + 1) {
        push_error(result, "route_time_invalid", "路径时间必须逐步递增且不能跳时刻",
                   order.order_id, {}, amr.amr_id, {}, step.position, previous.position,
                   step.time, previous.time, std::nullopt, std::nullopt,
                   static_cast<int>(index), static_cast<int>(index - 1));
      }
      const int distance = manhattan(previous.position, step.position);
      const bool same = distance == 0;
      const bool moving = distance == 1;
      if (step.action == RouteAction::kMove) {
        const bool aligned = valid_heading(step.heading) &&
                             same_cell(forward_cell(previous.position, step.heading),
                                       step.position);
        if (!moving || step.heading != previous.heading || !aligned) {
          push_error(result, "route_action_invalid", "move 必须沿当前朝向前进一格且保持朝向",
                     order.order_id, {}, amr.amr_id, {}, step.position, previous.position,
                     step.time, previous.time, std::nullopt, std::nullopt,
                     static_cast<int>(index), static_cast<int>(index - 1));
        } else {
          ++derived.move_count;
        }
      } else if (step.action == RouteAction::kTurnLeft) {
        if (!same || step.heading != left_heading(previous.heading)) {
          push_error(result, "route_action_invalid", "turn_left 必须原地按 90 度左转",
                     order.order_id, {}, amr.amr_id, {}, step.position, previous.position,
                     step.time, previous.time, std::nullopt, std::nullopt,
                     static_cast<int>(index), static_cast<int>(index - 1));
        }
      } else if (step.action == RouteAction::kTurnRight) {
        if (!same || step.heading != right_heading(previous.heading)) {
          push_error(result, "route_action_invalid", "turn_right 必须原地按 90 度右转",
                     order.order_id, {}, amr.amr_id, {}, step.position, previous.position,
                     step.time, previous.time, std::nullopt, std::nullopt,
                     static_cast<int>(index), static_cast<int>(index - 1));
        }
      } else if (step.action == RouteAction::kWait) {
        if (!same || step.heading != previous.heading) {
          push_error(result, "route_action_invalid", "wait 必须保持栅格和朝向不变",
                     order.order_id, {}, amr.amr_id, {}, step.position, previous.position,
                     step.time, previous.time, std::nullopt, std::nullopt,
                     static_cast<int>(index), static_cast<int>(index - 1));
        }
      } else {
        push_error(result, "route_action_invalid", "start 只能出现在路径首元素",
                   order.order_id, {}, amr.amr_id, {}, step.position, previous.position,
                   step.time, previous.time, std::nullopt, std::nullopt,
                   static_cast<int>(index), static_cast<int>(index - 1));
      }
      if (std::isfinite(previous.g_cost) && std::isfinite(step.g_cost) &&
          step.g_cost + kEpsilon < previous.g_cost) {
        push_error(result, "route_cost_invalid", "路径累计代价不能倒退", order.order_id, {},
                   amr.amr_id, {}, step.position, previous.position, step.time, previous.time,
                   step.g_cost, previous.g_cost, static_cast<int>(index),
                   static_cast<int>(index - 1));
      }
      if (!same && !movement_allowed(normalized, previous.position, step.position)) {
        const EdgeKey forward = edge_key(previous.position, step.position);
        const EdgeKey reverse = edge_key(step.position, previous.position);
        if (normalized.blocked_edges.count(forward) != 0U) {
          push_error(result, "forbidden_edge_traversed", "路径穿越禁行边", order.order_id, {},
                     amr.amr_id, {}, previous.position, step.position, previous.time,
                     step.time, std::nullopt, std::nullopt, static_cast<int>(index),
                     static_cast<int>(index - 1));
        } else if (normalized.one_way_edges.count(reverse) != 0U &&
                   normalized.one_way_edges.count(forward) == 0U) {
          push_error(result, "one_way_violation", "路径逆行通过单向边", order.order_id, {},
                     amr.amr_id, {}, previous.position, step.position, previous.time,
                     step.time, std::nullopt, std::nullopt, static_cast<int>(index),
                     static_cast<int>(index - 1));
        } else {
          push_error(result, "route_action_invalid", "路径移动跨越了非法边", order.order_id,
                     {}, amr.amr_id, {}, previous.position, step.position, previous.time,
                     step.time, std::nullopt, std::nullopt, static_cast<int>(index),
                     static_cast<int>(index - 1));
        }
      }
    } else if (step.action != RouteAction::kStart) {
      push_error(result, "route_action_invalid", "路径首元素 action 必须为 start",
                 order.order_id, {}, amr.amr_id, {}, step.position, {}, step.time,
                 std::nullopt, std::nullopt, std::nullopt, static_cast<int>(index));
    }
    if (same_cell(step.position, pickup_it->second)) {
      // 首次踏上 pickup 格可以早于 release_time（A* 预定位后等待）。
      // 装卸事件时刻必须等于 route.pickup_time，且当时仍停在 pickup。
      if (first_pickup_index < 0) {
        first_pickup_index = static_cast<int>(index);
      }
      if (step.time == route.pickup_time) {
        pickup_event_index = static_cast<int>(index);
      }
    }
  }

  // pickup 允许在首状态发生；dropoff 必须是路径终点，符合 P0-09 的完整路线
  // 语义。若未来引入装卸动作，可以在此处扩展服务占用区间而不改变路径安全检查。
  // pickup 事件对齐 A*：goal acceptance 要求 time>=release_time，允许提前到达
  // 后在工位等待。Validator 不能把“首次踏上 pickup 格”当成装货时刻。
  if (first_pickup_index < 0) {
    push_error(result, "pickup_not_reached", "路线没有到达订单 pickup 工位", order.order_id,
               {}, amr.amr_id, {}, pickup_it->second);
  } else if (pickup_event_index < 0) {
    push_error(result, "pickup_time_mismatch",
               "pickup_time 必须对应路径上停留在 pickup 工位的时刻", order.order_id, {},
               amr.amr_id, {}, pickup_it->second, {}, route.pickup_time,
               route.path[first_pickup_index].time, route.pickup_time,
               route.path[first_pickup_index].time, first_pickup_index);
  }
  derived.pickup_index = pickup_event_index >= 0 ? pickup_event_index : first_pickup_index;
  if (!same_cell(route.path.back().position, dropoff_it->second)) {
    push_error(result, "dropoff_not_reached", "路线终点不是订单 dropoff 工位", order.order_id,
               {}, amr.amr_id, {}, route.path.back().position, dropoff_it->second,
               route.path.back().time, std::nullopt, std::nullopt, std::nullopt,
               static_cast<int>(route.path.size() - 1));
  } else {
    derived.dropoff_index = static_cast<int>(route.path.size() - 1);
  }

  const int actual_pickup_time = pickup_event_index < 0
                                     ? -1
                                     : route.path[pickup_event_index].time;
  const int actual_dropoff_time = derived.dropoff_index < 0
                                      ? -1
                                      : route.path[derived.dropoff_index].time;
  if (pickup_event_index >= 0 && route.pickup_time != actual_pickup_time) {
    push_error(result, "pickup_time_mismatch", "pickup_time 必须对应路径上停留在 pickup 工位的时刻",
               order.order_id, {}, amr.amr_id, {}, pickup_it->second,
               {}, route.pickup_time, actual_pickup_time, route.pickup_time,
               actual_pickup_time, pickup_event_index);
  }
  if (derived.dropoff_index >= 0 && route.dropoff_time != actual_dropoff_time) {
    push_error(result, "dropoff_time_mismatch", "dropoff_time 与路径终点时刻不一致",
               order.order_id, {}, amr.amr_id, {}, dropoff_it->second,
               {}, route.dropoff_time, actual_dropoff_time, route.dropoff_time,
               actual_dropoff_time, derived.dropoff_index);
  }
  if (actual_pickup_time >= 0 && actual_pickup_time < order.release_time) {
    push_error(result, "pickup_before_release", "pickup 早于订单 release_time", order.order_id,
               {}, amr.amr_id, {}, pickup_it->second, {}, actual_pickup_time,
               order.release_time, actual_pickup_time, order.release_time,
               derived.pickup_index);
  }
  if (actual_dropoff_time >= 0 && actual_dropoff_time > order.deadline) {
    push_error(result, "dropoff_after_deadline", "dropoff 晚于订单 deadline", order.order_id,
               {}, amr.amr_id, {}, dropoff_it->second, {}, actual_dropoff_time,
               order.deadline, actual_dropoff_time, order.deadline, derived.dropoff_index);
  }

  if (derived.pickup_index >= 0) {
    const double load_after_pickup = amr.load + route.payload_kg;
    if (load_after_pickup > request.config.maximum_load_kg + kEpsilon) {
      push_error(result, "load_capacity_exceeded", "pickup 后载荷超过 AMR 最大载荷",
                 order.order_id, {}, amr.amr_id, {}, pickup_it->second,
                 {}, actual_pickup_time, std::nullopt, load_after_pickup,
                 request.config.maximum_load_kg, derived.pickup_index);
    }
  }
  if (amr.battery <= request.config.critical_battery_threshold_percent + kEpsilon) {
    push_error(result, "amr_battery_critical", "AMR 电量处于临界阈值，禁止执行普通运输",
               order.order_id, {}, amr.amr_id, {}, amr.position, {}, request.start_time,
               std::nullopt, amr.battery, request.config.critical_battery_threshold_percent);
  } else if (amr.battery <= request.config.new_task_battery_threshold_percent + kEpsilon) {
    push_error(result, "amr_battery_below_new_task_threshold",
               "AMR 电量不高于普通新任务阈值", order.order_id, {}, amr.amr_id, {},
               amr.position, {}, request.start_time, std::nullopt, amr.battery,
               request.config.new_task_battery_threshold_percent);
  }
  const double remaining_battery = amr.battery -
                                   static_cast<double>(derived.move_count) *
                                       request.config.energy_per_cell_percent;
  if (remaining_battery + kEpsilon < request.config.battery_safety_reserve_percent) {
    push_error(result, "battery_safety_reserve_breached", "路线结束电量低于安全余量",
               order.order_id, {}, amr.amr_id, {}, route.path.back().position,
               {}, route.path.back().time, std::nullopt, remaining_battery,
               request.config.battery_safety_reserve_percent,
               static_cast<int>(route.path.size() - 1));
  }
}

void validate_routes(const FleetPlanRequest& request,
                     NormalizedPlan& normalized,
                     ValidationResult& result) {
  normalized.derived_routes.resize(request.routes.size());
  for (std::size_t index = 0; index < request.routes.size(); ++index) {
    const auto& route = request.routes[index];
    if (route.amr_id.empty()) {
      push_error(result, "unknown_route_amr", "路线 amr_id 不能为空", route.order_id);
    } else if (normalized.amrs.count(route.amr_id) == 0U) {
      push_error(result, "unknown_route_amr", "路线引用了未知 AMR", route.order_id, {},
                 route.amr_id);
    } else if (!normalized.route_by_amr.emplace(route.amr_id, index).second) {
      push_error(result, "duplicate_route_amr", "一台 AMR 不能同时执行两条路线",
                 route.order_id, {}, route.amr_id);
    }
    if (route.order_id.empty()) {
      push_error(result, "unknown_route_order", "路线 order_id 不能为空", {}, {},
                 route.amr_id);
    } else if (normalized.orders.count(route.order_id) == 0U) {
      push_error(result, "unknown_route_order", "路线引用了未知订单", route.order_id, {},
                 route.amr_id);
    } else if (!normalized.route_by_order.emplace(route.order_id, index).second) {
      push_error(result, "duplicate_route_order", "一个订单不能同时执行两条路线",
                 route.order_id, {}, route.amr_id);
    }
    validate_route_status_and_payload(request, route, normalized.amrs, normalized.orders, result);
    validate_route_path(request, normalized, index, result);
  }

  validate_completed_order_conflicts(request, normalized, result);
  for (const auto& entry : normalized.orders) {
    if (normalized.completed_orders.count(entry.first) != 0U) continue;
    if (normalized.route_by_order.count(entry.first) == 0U) {
      push_error(result, "missing_route", "当前未完成订单没有对应执行路线", entry.first);
    }
  }
}

void validate_task_dependencies(const FleetPlanRequest& request,
                                const NormalizedPlan& normalized,
                                ValidationResult& result) {
  for (const auto& entry : normalized.orders) {
    const auto& order = entry.second;
    const auto route_it = normalized.route_by_order.find(order.order_id);
    if (route_it == normalized.route_by_order.end()) continue;
    const auto& route = request.routes[route_it->second];
    const auto& derived = normalized.derived_routes[route_it->second];
    const int pickup_time = derived.pickup_index < 0
                                ? route.pickup_time
                                : route.path[derived.pickup_index].time;
    for (const auto& dependency : order.dependencies) {
      if (normalized.completed_orders.count(dependency) != 0U) continue;
      const auto dep_route_it = normalized.route_by_order.find(dependency);
      if (dep_route_it == normalized.route_by_order.end()) {
        push_error(result, "task_dependency_unplanned", "前置订单没有执行路线", order.order_id,
                   dependency, route.amr_id);
        continue;
      }
      const auto& dep_route = request.routes[dep_route_it->second];
      const auto& dep_derived = normalized.derived_routes[dep_route_it->second];
      const int dependency_dropoff = dep_derived.dropoff_index < 0
                                         ? dep_route.dropoff_time
                                         : dep_route.path[dep_derived.dropoff_index].time;
      if (pickup_time < 0 || dependency_dropoff < 0 || dependency_dropoff > pickup_time) {
        const auto dep_order = normalized.orders.find(dependency);
        const GridPosition* coordinate = nullptr;
        if (dep_order != normalized.orders.end()) {
          const auto location = normalized.locations.find(dep_order->second.dropoff);
          if (location != normalized.locations.end()) coordinate = &location->second;
        }
        push_error(result, "task_dependency_time_order",
                   "当前订单 pickup 早于前置订单 dropoff", order.order_id, dependency,
                   route.amr_id, dep_route.amr_id,
                   coordinate == nullptr ? std::nullopt
                                          : std::optional<GridPosition>(*coordinate),
                   {}, pickup_time, dependency_dropoff, pickup_time, dependency_dropoff);
      }
    }
  }
}

void validate_workstation_capacity(const FleetPlanRequest& request,
                                   const NormalizedPlan& normalized,
                                   ValidationResult& result) {
  std::set<std::string> invalid_capacity_stations;
  std::vector<StationEvent> events;
  for (std::size_t index = 0; index < request.routes.size(); ++index) {
    const auto& route = request.routes[index];
    const auto& derived = normalized.derived_routes[index];
    const auto order_it = normalized.orders.find(route.order_id);
    if (order_it == normalized.orders.end() || derived.pickup_index < 0 ||
        derived.dropoff_index < 0 || route.path.empty()) {
      continue;
    }
    const auto pickup_it = normalized.locations.find(order_it->second.pickup);
    const auto dropoff_it = normalized.locations.find(order_it->second.dropoff);
    if (pickup_it != normalized.locations.end()) {
      events.push_back(StationEvent{order_it->second.pickup, route.order_id, route.amr_id,
                                    pickup_it->second, route.path[derived.pickup_index].time});
    }
    if (dropoff_it != normalized.locations.end()) {
      events.push_back(StationEvent{order_it->second.dropoff, route.order_id, route.amr_id,
                                    dropoff_it->second, route.path[derived.dropoff_index].time});
    }
  }
  std::sort(events.begin(), events.end(), [](const StationEvent& left, const StationEvent& right) {
    return std::tie(left.station_id, left.time, left.amr_id, left.order_id) <
           std::tie(right.station_id, right.time, right.amr_id, right.order_id);
  });

  // 先收集并稳定排序服务事件，再检查非法容量；若工位本次确实被使用，错误会
  // 绑定排序后的首个任务/AMR，否则至少保留工位坐标和观测容量，避免配置错误
  // 只有一句无法定位的文字。后续引入服务时长时仍可沿用同一证据锚点。
  for (const auto& entry : request.workstation_capacities) {
    if (!entry.first.empty() && entry.second > 0) continue;
    invalid_capacity_stations.insert(entry.first);
    const auto event = std::find_if(
        events.begin(), events.end(), [&](const StationEvent& candidate) {
          return candidate.station_id == entry.first;
        });
    const auto location = normalized.locations.find(entry.first);
    if (event != events.end()) {
      push_error(result, "workstation_capacity_config_missing",
                 entry.first.empty() ? "工位 ID 不能为空" : "工位容量必须是正整数",
                 event->order_id, {}, event->amr_id, {}, event->coordinate, {}, event->time,
                 std::nullopt, static_cast<double>(entry.second), 1.0);
    } else {
      push_error(result, "workstation_capacity_config_missing",
                 entry.first.empty() ? "工位 ID 不能为空" : "工位容量必须是正整数", {}, {},
                 {}, {}, location == normalized.locations.end()
                           ? std::nullopt
                           : std::optional<GridPosition>(location->second), {}, std::nullopt,
                 std::nullopt, static_cast<double>(entry.second), 1.0);
    }
  }

  std::size_t start = 0;
  while (start < events.size()) {
    std::size_t end = start + 1;
    while (end < events.size() && events[end].station_id == events[start].station_id &&
           events[end].time == events[start].time) {
      ++end;
    }
    int capacity = request.config.default_workstation_capacity;
    const auto configured = request.workstation_capacities.find(events[start].station_id);
    if (configured != request.workstation_capacities.end()) capacity = configured->second;
    if (invalid_capacity_stations.count(events[start].station_id) != 0U) {
      start = end;
      continue;
    }
    if (capacity <= 0) {
      push_error(result, "workstation_capacity_config_missing",
                 "工位容量必须是正整数", events[start].order_id, {}, events[start].amr_id,
                 {}, events[start].coordinate, {}, events[start].time, std::nullopt,
                 static_cast<double>(capacity), 1.0);
    } else if (static_cast<int>(end - start) > capacity) {
      for (std::size_t index = start + static_cast<std::size_t>(capacity); index < end; ++index) {
        const auto& primary = events[start];
        const auto& conflicting = events[index];
        push_error(result, "workstation_capacity_exceeded",
                   "同一离散时刻工位服务数量超过容量", conflicting.order_id, primary.order_id,
                   conflicting.amr_id, primary.amr_id, conflicting.coordinate,
                   primary.coordinate, conflicting.time, primary.time,
                   static_cast<double>(end - start), static_cast<double>(capacity));
      }
    }
    start = end;
  }
}

GridPosition position_at(const AMRState& amr,
                         const FleetPlanRoute* route,
                         int time) {
  if (route == nullptr || route->path.empty() || time < route->path.front().time) {
    return amr.position;
  }
  if (time >= route->path.back().time) return route->path.back().position;
  const int offset = time - route->path.front().time;
  if (offset >= 0 && static_cast<std::size_t>(offset) < route->path.size()) {
    return route->path[static_cast<std::size_t>(offset)].position;
  }
  // 非连续路径已经由单车规则报告；这里用相邻时间搜索保持跨车冲突检查
  // 的失败行为确定，不让一个坏路径导致未定义索引或崩溃。
  for (const auto& step : route->path) {
    if (step.time == time) return step.position;
  }
  return route->path.back().position;
}

int path_index_at(const FleetPlanRoute* route, int time) {
  if (route == nullptr || route->path.empty() || time < route->path.front().time ||
      time > route->path.back().time) {
    return -1;
  }
  const int offset = time - route->path.front().time;
  if (offset >= 0 && static_cast<std::size_t>(offset) < route->path.size() &&
      route->path[static_cast<std::size_t>(offset)].time == time) {
    return offset;
  }
  for (std::size_t index = 0; index < route->path.size(); ++index) {
    if (route->path[index].time == time) return static_cast<int>(index);
  }
  return -1;
}

void validate_fleet_conflicts(const FleetPlanRequest& request,
                              const NormalizedPlan& normalized,
                              ValidationResult& result) {
  std::vector<std::string> amr_ids;
  amr_ids.reserve(normalized.amrs.size());
  for (const auto& entry : normalized.amrs) amr_ids.push_back(entry.first);

  for (std::size_t left_index = 0; left_index < amr_ids.size(); ++left_index) {
    const auto& left_id = amr_ids[left_index];
    const auto& left_amr = normalized.amrs.at(left_id);
    const auto left_route_it = normalized.route_by_amr.find(left_id);
    const FleetPlanRoute* left_route = left_route_it == normalized.route_by_amr.end()
                                           ? nullptr
                                           : &request.routes[left_route_it->second];
    const std::string left_order = left_route == nullptr ? "" : left_route->order_id;
    for (std::size_t right_index = left_index + 1; right_index < amr_ids.size(); ++right_index) {
      const auto& right_id = amr_ids[right_index];
      const auto& right_amr = normalized.amrs.at(right_id);
      const auto right_route_it = normalized.route_by_amr.find(right_id);
      const FleetPlanRoute* right_route = right_route_it == normalized.route_by_amr.end()
                                             ? nullptr
                                             : &request.routes[right_route_it->second];
      const std::string right_order = right_route == nullptr ? "" : right_route->order_id;
      for (int time = request.start_time; time <= request.max_time; ++time) {
        const GridPosition left_cell = position_at(left_amr, left_route, time);
        const GridPosition right_cell = position_at(right_amr, right_route, time);
        const int separation = manhattan(left_cell, right_cell);
        const int left_path_index = path_index_at(left_route, time);
        const int right_path_index = path_index_at(right_route, time);
        if (same_cell(left_cell, right_cell)) {
          push_error(result, "vertex_conflict", "两台 AMR 在同一时刻占用同一顶点",
                     left_order, right_order, left_id, right_id, left_cell, right_cell, time,
                     time, 0.0, 0.0, left_path_index, right_path_index);
        } else if (separation < request.config.minimum_safety_distance_cells) {
          push_error(result, "safety_distance_breached", "两台 AMR 的曼哈顿安全距离不足",
                     left_order, right_order, left_id, right_id, left_cell, right_cell, time,
                     time, static_cast<double>(separation),
                     static_cast<double>(request.config.minimum_safety_distance_cells),
                     left_path_index, right_path_index);
        }
        if (time == request.max_time) continue;
        const GridPosition left_next = position_at(left_amr, left_route, time + 1);
        const GridPosition right_next = position_at(right_amr, right_route, time + 1);
        if (same_cell(left_cell, right_next) && same_cell(right_cell, left_next) &&
            !same_cell(left_cell, left_next)) {
          push_error(result, "swap_edge_conflict", "两台 AMR 在同一时段交换相邻边",
                     left_order, right_order, left_id, right_id, left_cell, right_cell, time,
                     time + 1, 1.0, 0.0, left_path_index,
                     path_index_at(right_route, time + 1));
        }
      }
    }
  }
}

bool optional_int_less(const std::optional<int>& left,
                       const std::optional<int>& right) {
  const int left_value = left.has_value() ? *left : std::numeric_limits<int>::max();
  const int right_value = right.has_value() ? *right : std::numeric_limits<int>::max();
  return left_value < right_value;
}

bool optional_position_less(const std::optional<GridPosition>& left,
                            const std::optional<GridPosition>& right) {
  const auto left_value = left.has_value()
                              ? std::make_pair(left->x, left->y)
                              : std::make_pair(std::numeric_limits<int>::max(),
                                               std::numeric_limits<int>::max());
  const auto right_value = right.has_value()
                               ? std::make_pair(right->x, right->y)
                               : std::make_pair(std::numeric_limits<int>::max(),
                                                std::numeric_limits<int>::max());
  return left_value < right_value;
}

bool optional_position_equal(const std::optional<GridPosition>& left,
                             const std::optional<GridPosition>& right) {
  if (!left.has_value() || !right.has_value()) return left.has_value() == right.has_value();
  return left->x == right->x && left->y == right->y;
}

void sort_errors(ValidationResult& result) {
  std::sort(result.errors.begin(), result.errors.end(), [](const ValidationEvidence& left,
                                                           const ValidationEvidence& right) {
    if (left.code != right.code) return left.code < right.code;
    if (left.time != right.time) return optional_int_less(left.time, right.time);
    if (left.amr_id != right.amr_id) return left.amr_id < right.amr_id;
    if (left.related_amr_id != right.related_amr_id) {
      return left.related_amr_id < right.related_amr_id;
    }
    if (left.order_id != right.order_id) return left.order_id < right.order_id;
    if (left.related_order_id != right.related_order_id) {
      return left.related_order_id < right.related_order_id;
    }
    if (!optional_position_equal(left.coordinate, right.coordinate) &&
        optional_position_less(left.coordinate, right.coordinate)) {
      return true;
    }
    if (!optional_position_equal(left.coordinate, right.coordinate)) return false;
    if (left.path_index != right.path_index) return left.path_index < right.path_index;
    if (left.related_path_index != right.related_path_index) {
      return left.related_path_index < right.related_path_index;
    }
    return left.message < right.message;
  });
}

}  // namespace

const std::vector<ValidationErrorDefinition>& error_dictionary() noexcept {
  // 按 code 字典序维护，find_definition 使用二分查找；文档和 JSON CLI 都从这
  // 一份表生成，避免实现新增错误码后忘记更新错误字典。
  static const std::vector<ValidationErrorDefinition> definitions = {
      {"amr_battery_below_new_task_threshold", "battery",
       "AMR 初始电量不高于普通新任务阈值", "order_id,amr_id,coordinate,time,observed,limit"},
      {"amr_battery_critical", "battery",
       "AMR 初始电量处于临界阈值", "order_id,amr_id,coordinate,time,observed,limit"},
      {"amr_unavailable", "amr_state",
       "AMR 不是 IDLE/HEALTHY/ONLINE", "amr_id,coordinate,time"},
      {"battery_safety_reserve_breached", "battery",
       "路线结束电量低于安全余量", "order_id,amr_id,coordinate,time,observed,limit"},
      {"dropoff_after_deadline", "time_window",
       "dropoff 晚于订单 deadline", "order_id,amr_id,coordinate,time,observed,limit"},
      {"dropoff_location_missing", "location_snapshot",
       "订单 dropoff 工位不在位置快照中", "order_id"},
      {"dropoff_not_reached", "route_geometry",
       "路线终点不是 dropoff 工位", "order_id,amr_id,coordinate,related_coordinate,time"},
      {"dropoff_time_mismatch", "route_timestamps",
       "dropoff_time 与路线终点时刻不一致", "order_id,amr_id,coordinate,time,related_time"},
      {"duplicate_amr_id", "schema_identity", "AMR ID 重复", "amr_id"},
      {"duplicate_blocked_cell", "forbidden_zone", "禁行区坐标重复", "coordinate"},
      {"duplicate_blocked_edge", "forbidden_edge", "禁行边重复", "coordinate,related_coordinate"},
      {"duplicate_completed_order_id", "schema_identity", "已完成订单 ID 重复", "order_id"},
      {"duplicate_location_id", "schema_identity", "工位 ID 重复", "coordinate"},
      {"duplicate_one_way_edge", "one_way_edge", "单向边重复", "coordinate,related_coordinate"},
      {"duplicate_order_dependency", "task_dependency", "订单依赖重复", "order_id,related_order_id"},
      {"duplicate_order_id", "schema_identity", "订单 ID 重复", "order_id"},
      {"duplicate_route_amr", "route_assignment", "AMR 被分配多条路线", "order_id,amr_id"},
      {"duplicate_route_order", "route_assignment", "订单被规划多条路线", "order_id,amr_id"},
      {"environment_ref_empty", "request_identity", "环境快照引用为空", ""},
      {"forbidden_edge_traversed", "forbidden_edge",
       "路径穿越禁行边", "order_id,amr_id,coordinate,related_coordinate,time,related_time"},
      {"forbidden_zone_occupied", "forbidden_zone", "AMR、工位或路径进入禁行区", "order_id,amr_id,coordinate,time"},
      {"invalid_blocked_edge", "forbidden_edge", "禁行边不是地图内相邻边", "coordinate,related_coordinate"},
      {"invalid_completed_order_id", "schema_identity", "已完成订单 ID 不能为空", "order_id"},
      {"invalid_config", "validator_config", "Validator 配置非法", ""},
      {"invalid_map", "map_snapshot", "地图尺寸非法", ""},
      {"invalid_one_way_edge", "one_way_edge", "单向边不是地图内相邻边", "coordinate,related_coordinate"},
      {"invalid_order", "order_schema", "订单字段或时间窗非法", "order_id"},
      {"invalid_time_horizon", "time_horizon", "时间范围非法", "time,related_time"},
      {"load_capacity_exceeded", "load_capacity", "载荷超过最大容量", "order_id,amr_id,coordinate,time,observed,limit"},
      {"missing_route", "route_assignment", "未完成订单缺少路线", "order_id"},
      {"one_way_violation", "one_way_edge", "路径逆行通过单向边", "order_id,amr_id,coordinate,related_coordinate,time"},
      {"order_already_completed", "task_state", "已完成订单被再次规划", "order_id,amr_id"},
      {"order_dependency_cycle", "task_dependency", "订单依赖图存在循环", "order_id,related_order_id"},
      {"pickup_before_release", "time_window", "pickup 早于 release_time", "order_id,amr_id,coordinate,time,observed,limit"},
      {"pickup_location_missing", "location_snapshot", "订单 pickup 工位不在位置快照中", "order_id"},
      {"pickup_not_reached", "route_geometry", "路线没有到达 pickup 工位", "order_id,amr_id,coordinate"},
      {"pickup_time_mismatch", "route_timestamps", "pickup_time 必须对应路径上停留在 pickup 工位的时刻", "order_id,amr_id,coordinate,time,related_time"},
      {"route_action_invalid", "route_geometry", "路径动作与位置/朝向不一致", "order_id,amr_id,coordinate,related_coordinate,time"},
      {"route_cost_invalid", "route_cost", "路径累计代价非法或倒退", "order_id,amr_id,coordinate,time,observed,limit"},
      {"route_empty", "route_geometry", "路线为空", "order_id,amr_id"},
      {"route_heading_invalid", "route_geometry", "路径朝向非法", "order_id,amr_id,coordinate,time"},
      {"route_not_planned", "route_status", "上游路线不是 planned", "order_id,amr_id"},
      {"route_out_of_bounds", "map_boundary", "路径或资源坐标越界", "order_id,amr_id,coordinate,time"},
      {"route_payload_invalid", "load_capacity", "payload_kg 非法", "order_id,amr_id"},
      {"route_start_mismatch", "route_geometry", "路线首状态与 AMR 快照不一致", "order_id,amr_id,coordinate,time"},
      {"route_time_invalid", "route_timestamps", "路径时间不连续或越界", "order_id,amr_id,coordinate,time,related_time"},
      {"safety_distance_breached", "safety_distance", "两台 AMR 曼哈顿距离不足", "order_id,related_order_id,amr_id,related_amr_id,coordinate,related_coordinate,time,observed,limit"},
      {"stl_specification_violated", "stl_specification", "STL 规约在 gate 模式下被违反（message 含公式 id，observed 为鲁棒度，time 为最薄弱时刻）", "order_id,related_order_id,amr_id,related_amr_id,coordinate,related_coordinate,time,observed,limit"},
      {"swap_edge_conflict", "swap_edge_conflict", "两台 AMR 交换同一条边", "order_id,related_order_id,amr_id,related_amr_id,coordinate,related_coordinate,time,related_time"},
      {"task_dependency_time_order", "task_dependency", "依赖任务尚未在当前任务 pickup 前完成", "order_id,related_order_id,amr_id,related_amr_id,coordinate,time,related_time"},
      {"task_dependency_unplanned", "task_dependency", "依赖任务没有执行路线", "order_id,related_order_id,amr_id"},
      {"unknown_order_dependency", "task_dependency", "订单依赖未知任务", "order_id,related_order_id"},
      {"unknown_route_amr", "route_assignment", "路线引用未知 AMR", "order_id,amr_id"},
      {"unknown_route_order", "route_assignment", "路线引用未知订单", "order_id,amr_id"},
      {"vertex_conflict", "vertex_conflict", "两台 AMR 同时占用同一顶点", "order_id,related_order_id,amr_id,related_amr_id,coordinate,time"},
      {"workstation_capacity_config_missing", "workstation_capacity", "工位容量缺失或非法", "order_id,amr_id,coordinate,time,observed,limit"},
      {"workstation_capacity_exceeded", "workstation_capacity", "工位同一时刻服务数量超过容量", "order_id,related_order_id,amr_id,related_amr_id,coordinate,time,observed,limit"},
  };
  return definitions;
}

ValidationResult validate_fleet_plan(const FleetPlanRequest& request) {
  return validate_fleet_plan(request, nullptr);
}

ValidationResult validate_fleet_plan(const FleetPlanRequest& request,
                                     const stl::Specification* specification) {
  ValidationResult result;
  result.ruleset_version = request.ruleset_version;
  NormalizedPlan normalized;
  // 保留请求的地图视图，movement_allowed 等共享规则就不会复制一份可能漂移的
  // RouteMap；normalized 只保存排序后的索引和去重后的快速查找集合。
  normalized.map = request.map;
  validate_config(request, result);
  validate_map(request, normalized, result);
  validate_amrs(request, normalized, result);
  validate_orders(request, normalized, result);
  validate_routes(request, normalized, result);
  validate_task_dependencies(request, normalized, result);
  validate_workstation_capacity(request, normalized, result);
  validate_fleet_conflicts(request, normalized, result);

  if (specification != nullptr) {
    // P1-1 第二判定层：STL 监控器只读取原始请求，不读取 normalized 或上面的
    // errors，因此它的结论与规则层相互独立；gate 模式把每条违反实例追加成
    // 稳定错误证据，shadow 模式只把报告挂在结果上。
    result.stl = monitor_fleet_plan(request, *specification);
    if (specification->enforcement == stl::Enforcement::kGate) {
      for (const auto& instance : result.stl->results) {
        if (instance.satisfied) continue;
        std::string message = "STL 规约 " + instance.formula_id + " 被违反";
        if (instance.robustness.has_value()) {
          message += "，鲁棒度 " + std::to_string(*instance.robustness);
        } else {
          message += "，鲁棒度为空真/空假";
        }
        if (instance.weakest_time.has_value()) {
          message += "，最薄弱时刻 " + std::to_string(*instance.weakest_time);
        }
        push_error(result, "stl_specification_violated", message, instance.scope.order_id,
                   instance.scope.related_order_id, instance.scope.amr_id,
                   instance.scope.related_amr_id, instance.coordinate,
                   instance.related_coordinate, instance.weakest_time, std::nullopt,
                   instance.robustness, instance.robustness.has_value()
                                            ? std::optional<double>(0.0)
                                            : std::nullopt);
      }
    }
  }

  sort_errors(result);
  result.valid = result.errors.empty();
  result.status = result.valid ? "valid" : "invalid";
  return result;
}

}  // namespace amr::planner::validator
