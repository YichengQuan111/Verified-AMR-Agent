#include "fleet_plan_validator/fleet_plan_validator.hpp"
#include "fleet_plan_validator/stl_monitor.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <map>
#include <set>
#include <tuple>
#include <utility>

// P1-1：把 FleetPlanRequest 转换成 STL 监控器所需的离散时间信号轨迹，并按
// 规约逐作用域实例化公式。本文件刻意不复用 fleet_plan_validator.cpp 匿名命名
// 空间里的栅格 helper：两层若共享同一个带 Bug 的 helper，会同时给出同样错误的
// 结论，一致性核对就失去了“独立 oracle”的意义。这里的信号语义与规则层逐条
// 对齐（装货时刻、终点占用到 max_time、只按合法 move 扣电等），任何差异都
// 应通过一致性核对暴露，而不是在这里“修一下让它一致”。
namespace amr::planner::validator {
namespace {

using CellKey = std::pair<int, int>;
using EdgeKey = std::tuple<int, int, int, int>;

int manhattan(const GridPosition& left, const GridPosition& right) noexcept {
  const long long dx = static_cast<long long>(left.x) - static_cast<long long>(right.x);
  const long long dy = static_cast<long long>(left.y) - static_cast<long long>(right.y);
  const long long distance = (dx < 0 ? -dx : dx) + (dy < 0 ? -dy : dy);
  return distance > static_cast<long long>(std::numeric_limits<int>::max())
             ? std::numeric_limits<int>::max()
             : static_cast<int>(distance);
}

bool same_cell(const GridPosition& left, const GridPosition& right) noexcept {
  return left.x == right.x && left.y == right.y;
}

bool valid_heading(int heading) noexcept {
  return heading == 0 || heading == 90 || heading == 180 || heading == 270;
}

GridPosition forward_cell(const GridPosition& position, int heading) noexcept {
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

bool in_bounds(const RouteMap& map, const GridPosition& cell) noexcept {
  return cell.x >= 0 && cell.x < map.width && cell.y >= 0 && cell.y < map.height;
}

CellKey cell_key(const GridPosition& cell) noexcept {
  return {cell.x, cell.y};
}

EdgeKey edge_key(const GridPosition& from, const GridPosition& to) noexcept {
  return {from.x, from.y, to.x, to.y};
}

// 一台 AMR 在整个时间域上的逐时刻状态。没有路线的 AMR 全程停在初始位置，
// 有路线的 AMR 在路径结束后保持终点占用，与规则层 position_at 语义一致。
struct Timeline {
  std::vector<GridPosition> position;
  std::vector<int> moves;
  std::vector<double> edge_legal;
};

struct OrderTimes {
  int pickup_event{-1};      // 路径停在 pickup 且时间等于 route.pickup_time 的时刻
  int pickup_effective{-1};  // 事件时刻，否则首次踏上 pickup 的时刻（载荷/工位/依赖用）
  int dropoff_actual{-1};    // 路径终点位于 dropoff 时的终点时刻
};

struct PlanModel {
  int start_time{0};
  int max_time{0};
  std::size_t length{0};
  RouteMap map;
  std::map<std::string, AMRState> amrs;
  std::map<std::string, TransportOrder> orders;
  std::map<std::string, GridPosition> locations;
  std::set<CellKey> blocked_cells;
  std::set<EdgeKey> blocked_edges;
  std::set<EdgeKey> one_way_edges;
  std::set<std::string> completed_orders;
  std::map<std::string, std::size_t> route_by_amr;
  std::map<std::string, std::size_t> route_by_order;
  std::map<std::string, Timeline> timelines;
  std::map<std::string, OrderTimes> order_times;
  std::vector<GridPosition> charging_positions;
  std::map<std::string, double> global_parameters;
};

bool build_model(const FleetPlanRequest& request,
                 const stl::Specification& specification,
                 PlanModel& model,
                 std::string& skip_reason) {
  if (request.map.width <= 0 || request.map.height <= 0 || request.map.width > 100 ||
      request.map.height > 100) {
    skip_reason = "地图尺寸非法，无法构造信号轨迹";
    return false;
  }
  if (request.start_time < 0 || request.max_time < request.start_time ||
      request.max_time > 2000) {
    skip_reason = "时间域非法，无法构造信号轨迹";
    return false;
  }
  model.start_time = request.start_time;
  model.max_time = request.max_time;
  model.length = static_cast<std::size_t>(request.max_time - request.start_time) + 1U;
  model.map = request.map;

  for (const auto& amr : request.amrs) {
    if (amr.amr_id.empty()) continue;
    model.amrs.emplace(amr.amr_id, amr);
  }
  for (const auto& order : request.orders) {
    if (order.order_id.empty()) continue;
    model.orders.emplace(order.order_id, order);
  }
  for (const auto& location : request.locations) {
    if (location.location_id.empty()) continue;
    model.locations.emplace(location.location_id, location.position);
  }
  for (const auto& cell : request.map.blocked_cells) {
    if (in_bounds(request.map, cell)) model.blocked_cells.insert(cell_key(cell));
  }
  const auto collect_edges = [&](const std::vector<RouteEdge>& edges, std::set<EdgeKey>& out) {
    for (const auto& edge : edges) {
      if (in_bounds(request.map, edge.from) && in_bounds(request.map, edge.to) &&
          manhattan(edge.from, edge.to) == 1) {
        out.insert(edge_key(edge.from, edge.to));
      }
    }
  };
  collect_edges(request.map.blocked_edges, model.blocked_edges);
  collect_edges(request.map.one_way_edges, model.one_way_edges);
  for (const auto& completed : request.completed_order_ids) {
    if (!completed.empty()) model.completed_orders.insert(completed);
  }
  for (std::size_t index = 0; index < request.routes.size(); ++index) {
    const auto& route = request.routes[index];
    if (route.path.empty() || model.amrs.count(route.amr_id) == 0U ||
        model.orders.count(route.order_id) == 0U) {
      continue;
    }
    if (model.route_by_amr.count(route.amr_id) != 0U ||
        model.route_by_order.count(route.order_id) != 0U) {
      continue;  // 重复绑定属于结构错误，规则层已报告；STL 只看首条。
    }
    model.route_by_amr.emplace(route.amr_id, index);
    model.route_by_order.emplace(route.order_id, index);
  }
  for (const auto& id : specification.charging_location_ids) {
    const auto it = model.locations.find(id);
    if (it != model.locations.end()) model.charging_positions.push_back(it->second);
  }

  // 每台 AMR 的时间线：按路径顺序判断 move 是否合法（与规则层同一判据），
  // 再按时间轴展开，缺失时刻保持上一状态。
  for (const auto& entry : model.amrs) {
    const auto& amr = entry.second;
    Timeline timeline;
    timeline.position.assign(model.length, amr.position);
    timeline.moves.assign(model.length, 0);
    timeline.edge_legal.assign(model.length, 1.0);
    const auto route_it = model.route_by_amr.find(amr.amr_id);
    if (route_it != model.route_by_amr.end()) {
      const auto& path = request.routes[route_it->second].path;
      struct StepState {
        GridPosition position;
        int moves;
        double edge_legal;
      };
      std::map<int, StepState> by_time;
      int moves = 0;
      for (std::size_t index = 0; index < path.size(); ++index) {
        const auto& step = path[index];
        double edge_legal = 1.0;
        if (index > 0) {
          const auto& previous = path[index - 1];
          const int distance = manhattan(previous.position, step.position);
          if (step.action == RouteAction::kMove && distance == 1 &&
              step.heading == previous.heading && valid_heading(step.heading) &&
              same_cell(forward_cell(previous.position, step.heading), step.position)) {
            ++moves;
          }
          if (distance != 0) {
            const EdgeKey forward = edge_key(previous.position, step.position);
            const EdgeKey reverse = edge_key(step.position, previous.position);
            if (model.blocked_edges.count(forward) != 0U ||
                (model.one_way_edges.count(reverse) != 0U &&
                 model.one_way_edges.count(forward) == 0U)) {
              edge_legal = -1.0;
            }
          }
        }
        by_time.emplace(step.time, StepState{step.position, moves, edge_legal});
      }
      GridPosition current = amr.position;
      int current_moves = 0;
      for (std::size_t offset = 0; offset < model.length; ++offset) {
        const int time = model.start_time + static_cast<int>(offset);
        const auto step_it = by_time.find(time);
        if (step_it != by_time.end()) {
          current = step_it->second.position;
          current_moves = step_it->second.moves;
          timeline.edge_legal[offset] = step_it->second.edge_legal;
        }
        timeline.position[offset] = current;
        timeline.moves[offset] = current_moves;
      }
    }
    model.timelines.emplace(amr.amr_id, std::move(timeline));
  }

  // 订单事件时刻：装货事件必须“停在 pickup 且时间等于 route.pickup_time”，
  // 首次踏上 pickup 只作为兜底（与规则层 derived.pickup_index 相同）。
  for (const auto& entry : model.route_by_order) {
    const auto& route = request.routes[entry.second];
    const auto order_it = model.orders.find(route.order_id);
    const auto pickup_it = model.locations.find(order_it->second.pickup);
    const auto dropoff_it = model.locations.find(order_it->second.dropoff);
    if (pickup_it == model.locations.end() || dropoff_it == model.locations.end()) continue;
    OrderTimes times;
    int first_pickup = -1;
    for (const auto& step : route.path) {
      if (!same_cell(step.position, pickup_it->second)) continue;
      if (first_pickup < 0) first_pickup = step.time;
      if (step.time == route.pickup_time) times.pickup_event = step.time;
    }
    times.pickup_effective = times.pickup_event >= 0 ? times.pickup_event : first_pickup;
    if (same_cell(route.path.back().position, dropoff_it->second)) {
      times.dropoff_actual = route.path.back().time;
    }
    model.order_times.emplace(route.order_id, times);
  }

  model.global_parameters = {
      {"horizon", static_cast<double>(model.length - 1U)},
      {"maximum_load_kg", request.config.maximum_load_kg},
      {"energy_per_cell_percent", request.config.energy_per_cell_percent},
      {"battery_safety_reserve_percent", request.config.battery_safety_reserve_percent},
      {"new_task_battery_threshold_percent", request.config.new_task_battery_threshold_percent},
      {"critical_battery_threshold_percent", request.config.critical_battery_threshold_percent},
      {"minimum_safety_distance_cells",
       static_cast<double>(request.config.minimum_safety_distance_cells)},
  };
  return true;
}

stl::SignalTrace base_trace(const PlanModel& model) {
  stl::SignalTrace trace;
  trace.start_time = model.start_time;
  trace.length = model.length;
  trace.parameters = model.global_parameters;
  std::vector<double> time(model.length);
  for (std::size_t offset = 0; offset < model.length; ++offset) {
    time[offset] = static_cast<double>(offset);
  }
  trace.signals.emplace("t", std::move(time));
  return trace;
}

// “从未发生”的事件用 max_time + 1 表示：裕量在整个有限时间域内始终为负，
// F/U 形式的公式因此必然违反，而不会被空窗口误判成空真。
double never_time(const PlanModel& model) {
  return static_cast<double>(model.max_time) + 1.0;
}

std::vector<double> margin_signal(const PlanModel& model, int event_time) {
  const double anchor = event_time >= 0 ? static_cast<double>(event_time) : never_time(model);
  std::vector<double> values(model.length);
  for (std::size_t offset = 0; offset < model.length; ++offset) {
    values[offset] = static_cast<double>(model.start_time + static_cast<int>(offset)) - anchor;
  }
  return values;
}

struct ScopeInstance {
  stl::ScopeIdentity identity;
  stl::SignalTrace trace;
  // 作用域涉及的 AMR 时间线，用于给证据补坐标。
  const Timeline* primary_timeline{nullptr};
  const Timeline* related_timeline{nullptr};
};

std::vector<ScopeInstance> order_instances(const FleetPlanRequest& request, const PlanModel& model) {
  std::vector<ScopeInstance> instances;
  for (const auto& entry : model.order_times) {
    const auto& route = request.routes[model.route_by_order.at(entry.first)];
    const auto& order = model.orders.at(entry.first);
    ScopeInstance instance;
    instance.identity.kind = stl::ScopeKind::kOrder;
    instance.identity.order_id = entry.first;
    instance.identity.amr_id = route.amr_id;
    instance.trace = base_trace(model);
    instance.trace.signals.emplace("loaded_margin", margin_signal(model, entry.second.pickup_event));
    instance.trace.signals.emplace("delivered_margin",
                                   margin_signal(model, entry.second.dropoff_actual));
    instance.trace.parameters.emplace(
        "release_time", static_cast<double>(order.release_time - model.start_time));
    instance.trace.parameters.emplace("deadline",
                                      static_cast<double>(order.deadline - model.start_time));
    instance.trace.parameters.emplace("priority", static_cast<double>(order.priority));
    instance.primary_timeline = &model.timelines.at(route.amr_id);
    instances.push_back(std::move(instance));
  }
  return instances;
}

std::vector<ScopeInstance> amr_instances(const FleetPlanRequest& request, const PlanModel& model) {
  std::vector<ScopeInstance> instances;
  const double far_distance = static_cast<double>(model.map.width + model.map.height);
  for (const auto& entry : model.route_by_amr) {
    const auto& route = request.routes[entry.second];
    const auto& amr = model.amrs.at(entry.first);
    const auto& timeline = model.timelines.at(entry.first);
    const auto times_it = model.order_times.find(route.order_id);
    const int pickup_effective =
        times_it == model.order_times.end() ? -1 : times_it->second.pickup_effective;
    ScopeInstance instance;
    instance.identity.kind = stl::ScopeKind::kAmr;
    instance.identity.amr_id = entry.first;
    instance.identity.order_id = route.order_id;
    instance.trace = base_trace(model);
    std::vector<double> battery(model.length);
    std::vector<double> load(model.length);
    std::vector<double> blocked_distance(model.length);
    std::vector<double> boundary(model.length);
    std::vector<double> charging(model.length);
    std::vector<double> moves(model.length);
    for (std::size_t offset = 0; offset < model.length; ++offset) {
      const int time = model.start_time + static_cast<int>(offset);
      const GridPosition& cell = timeline.position[offset];
      battery[offset] = amr.battery - static_cast<double>(timeline.moves[offset]) *
                                          request.config.energy_per_cell_percent;
      load[offset] = amr.load + ((pickup_effective >= 0 && time >= pickup_effective)
                                     ? route.payload_kg
                                     : 0.0);
      double nearest = far_distance;
      for (const auto& blocked : model.blocked_cells) {
        nearest = std::min(nearest, static_cast<double>(manhattan(
                                        cell, GridPosition{blocked.first, blocked.second})));
      }
      blocked_distance[offset] = nearest;
      boundary[offset] = static_cast<double>(
          std::min({cell.x, cell.y, model.map.width - 1 - cell.x, model.map.height - 1 - cell.y}));
      charging[offset] = -1.0;
      for (const auto& station : model.charging_positions) {
        if (same_cell(station, cell)) {
          charging[offset] = 1.0;
          break;
        }
      }
      moves[offset] = static_cast<double>(timeline.moves[offset]);
    }
    instance.trace.signals.emplace("battery", std::move(battery));
    instance.trace.signals.emplace("load", std::move(load));
    instance.trace.signals.emplace("blocked_cell_distance", std::move(blocked_distance));
    instance.trace.signals.emplace("boundary_margin", std::move(boundary));
    instance.trace.signals.emplace("edge_legal", timeline.edge_legal);
    instance.trace.signals.emplace("at_charging_station", std::move(charging));
    instance.trace.signals.emplace("moves", std::move(moves));
    instance.trace.parameters.emplace("payload_kg", route.payload_kg);
    instance.trace.parameters.emplace("initial_battery", amr.battery);
    instance.primary_timeline = &timeline;
    instances.push_back(std::move(instance));
  }
  return instances;
}

std::vector<ScopeInstance> pair_instances(const FleetPlanRequest& request, const PlanModel& model) {
  std::vector<ScopeInstance> instances;
  std::vector<std::string> ids;
  for (const auto& entry : model.amrs) ids.push_back(entry.first);
  for (std::size_t left = 0; left < ids.size(); ++left) {
    for (std::size_t right = left + 1; right < ids.size(); ++right) {
      const auto& left_timeline = model.timelines.at(ids[left]);
      const auto& right_timeline = model.timelines.at(ids[right]);
      ScopeInstance instance;
      instance.identity.kind = stl::ScopeKind::kPair;
      instance.identity.amr_id = ids[left];
      instance.identity.related_amr_id = ids[right];
      const auto left_route = model.route_by_amr.find(ids[left]);
      const auto right_route = model.route_by_amr.find(ids[right]);
      if (left_route != model.route_by_amr.end()) {
        instance.identity.order_id = request.routes[left_route->second].order_id;
      }
      if (right_route != model.route_by_amr.end()) {
        instance.identity.related_order_id = request.routes[right_route->second].order_id;
      }
      instance.trace = base_trace(model);
      std::vector<double> distance(model.length);
      std::vector<double> no_swap(model.length, 1.0);
      for (std::size_t offset = 0; offset < model.length; ++offset) {
        distance[offset] = static_cast<double>(
            manhattan(left_timeline.position[offset], right_timeline.position[offset]));
        if (offset + 1 < model.length) {
          const auto& left_now = left_timeline.position[offset];
          const auto& left_next = left_timeline.position[offset + 1];
          const auto& right_now = right_timeline.position[offset];
          const auto& right_next = right_timeline.position[offset + 1];
          if (same_cell(left_now, right_next) && same_cell(right_now, left_next) &&
              !same_cell(left_now, left_next)) {
            no_swap[offset] = -1.0;
          }
        }
      }
      instance.trace.signals.emplace("pair_distance", std::move(distance));
      instance.trace.signals.emplace("no_edge_swap", std::move(no_swap));
      instance.primary_timeline = &left_timeline;
      instance.related_timeline = &right_timeline;
      instances.push_back(std::move(instance));
    }
  }
  return instances;
}

std::vector<ScopeInstance> station_instances(const FleetPlanRequest& request,
                                             const PlanModel& model) {
  std::map<std::string, std::vector<int>> events;
  for (const auto& entry : model.order_times) {
    const auto& times = entry.second;
    if (times.pickup_effective < 0 || times.dropoff_actual < 0) continue;
    const auto& order = model.orders.at(entry.first);
    if (model.locations.count(order.pickup) != 0U) {
      events[order.pickup].push_back(times.pickup_effective);
    }
    if (model.locations.count(order.dropoff) != 0U) {
      events[order.dropoff].push_back(times.dropoff_actual);
    }
  }
  std::set<std::string> station_ids;
  for (const auto& entry : request.workstation_capacities) {
    if (!entry.first.empty()) station_ids.insert(entry.first);
  }
  for (const auto& entry : events) station_ids.insert(entry.first);

  std::vector<ScopeInstance> instances;
  for (const auto& station : station_ids) {
    int capacity = request.config.default_workstation_capacity;
    const auto configured = request.workstation_capacities.find(station);
    if (configured != request.workstation_capacities.end()) capacity = configured->second;
    // 非正容量是配置错误（规则层 workstation_capacity_config_missing），不在
    // STL 的容量约束里重复判定。
    if (capacity <= 0) continue;
    ScopeInstance instance;
    instance.identity.kind = stl::ScopeKind::kStation;
    instance.identity.station_id = station;
    instance.trace = base_trace(model);
    std::vector<double> occupancy(model.length, 0.0);
    const auto event_it = events.find(station);
    if (event_it != events.end()) {
      for (const int time : event_it->second) {
        if (time < model.start_time || time > model.max_time) continue;
        occupancy[static_cast<std::size_t>(time - model.start_time)] += 1.0;
      }
    }
    instance.trace.signals.emplace("occupancy", std::move(occupancy));
    instance.trace.parameters.emplace("capacity", static_cast<double>(capacity));
    instances.push_back(std::move(instance));
  }
  return instances;
}

std::vector<ScopeInstance> dependency_instances(const FleetPlanRequest& request,
                                                const PlanModel& model) {
  std::vector<ScopeInstance> instances;
  for (const auto& entry : model.route_by_order) {
    const auto& route = request.routes[entry.second];
    const auto& order = model.orders.at(entry.first);
    const auto times_it = model.order_times.find(entry.first);
    int dependent_loaded = times_it != model.order_times.end() &&
                                   times_it->second.pickup_effective >= 0
                               ? times_it->second.pickup_effective
                               : route.pickup_time;
    std::set<std::string> seen;
    for (const auto& dependency : order.dependencies) {
      if (dependency.empty() || dependency == order.order_id || !seen.insert(dependency).second) {
        continue;
      }
      if (model.completed_orders.count(dependency) != 0U) continue;
      if (model.orders.count(dependency) == 0U) continue;  // 未知依赖属于结构错误
      int prerequisite_delivered = -1;
      std::string prerequisite_amr;
      const auto dep_route = model.route_by_order.find(dependency);
      if (dep_route != model.route_by_order.end()) {
        prerequisite_amr = request.routes[dep_route->second].amr_id;
        const auto dep_times = model.order_times.find(dependency);
        prerequisite_delivered = dep_times != model.order_times.end() &&
                                         dep_times->second.dropoff_actual >= 0
                                     ? dep_times->second.dropoff_actual
                                     : request.routes[dep_route->second].dropoff_time;
      }
      ScopeInstance instance;
      instance.identity.kind = stl::ScopeKind::kDependency;
      instance.identity.order_id = order.order_id;
      instance.identity.related_order_id = dependency;
      instance.identity.amr_id = route.amr_id;
      instance.identity.related_amr_id = prerequisite_amr;
      instance.trace = base_trace(model);
      instance.trace.signals.emplace("dependent_loaded_margin",
                                     margin_signal(model, dependent_loaded));
      instance.trace.signals.emplace("prerequisite_delivered_margin",
                                     margin_signal(model, prerequisite_delivered));
      instance.primary_timeline = &model.timelines.at(route.amr_id);
      if (!prerequisite_amr.empty()) {
        instance.related_timeline = &model.timelines.at(prerequisite_amr);
      }
      instances.push_back(std::move(instance));
    }
  }
  return instances;
}

std::optional<GridPosition> position_at(const Timeline* timeline,
                                        const PlanModel& model,
                                        const std::optional<int>& time) {
  if (timeline == nullptr || !time.has_value()) return std::nullopt;
  const int offset = *time - model.start_time;
  if (offset < 0 || static_cast<std::size_t>(offset) >= timeline->position.size()) {
    return std::nullopt;
  }
  return timeline->position[static_cast<std::size_t>(offset)];
}

}  // namespace

stl::MonitorReport monitor_fleet_plan(const FleetPlanRequest& request,
                                      const stl::Specification& specification) {
  stl::MonitorReport report;
  report.spec_id = specification.spec_id;
  report.spec_version = specification.spec_version;
  report.enforcement = specification.enforcement;
  report.formula_count = specification.formulas.size();

  PlanModel model;
  std::string skip_reason;
  if (!build_model(request, specification, model, skip_reason)) {
    report.status = "skipped";
    report.satisfied = false;
    report.skip_reason = skip_reason;
    return report;
  }

  // 作用域实例只构造一次，再按规约顺序对每条公式求值，输出顺序因此固定：
  // 公式按文件顺序，实例按 ID 字典序。
  std::map<stl::ScopeKind, std::vector<ScopeInstance>> instances;
  instances.emplace(stl::ScopeKind::kOrder, order_instances(request, model));
  instances.emplace(stl::ScopeKind::kAmr, amr_instances(request, model));
  instances.emplace(stl::ScopeKind::kPair, pair_instances(request, model));
  instances.emplace(stl::ScopeKind::kStation, station_instances(request, model));
  instances.emplace(stl::ScopeKind::kDependency, dependency_instances(request, model));

  for (const auto& formula : specification.formulas) {
    for (const auto& instance : instances.at(formula.scope)) {
      const stl::Evaluation evaluation = stl::evaluate(formula.formula, instance.trace);
      stl::InstanceResult result;
      result.formula_id = formula.id;
      result.scope = instance.identity;
      result.satisfied = evaluation.satisfied;
      result.vacuous = evaluation.vacuous;
      if (!evaluation.vacuous) result.robustness = evaluation.robustness;
      result.weakest_time = evaluation.weakest_time;
      result.coordinate = position_at(instance.primary_timeline, model, evaluation.weakest_time);
      result.related_coordinate =
          position_at(instance.related_timeline, model, evaluation.weakest_time);
      result.narrow_pass = result.satisfied && result.robustness.has_value() &&
                           formula.warn_below.has_value() &&
                           *result.robustness < *formula.warn_below;
      if (!result.satisfied) ++report.violated_count;
      if (result.narrow_pass) ++report.narrow_pass_count;
      if (result.robustness.has_value() &&
          (!report.min_robustness.has_value() || *result.robustness < *report.min_robustness)) {
        report.min_robustness = result.robustness;
        report.min_robustness_formula_id = formula.id;
        report.min_robustness_scope = instance.identity;
      }
      report.results.push_back(std::move(result));
    }
  }
  report.instance_count = report.results.size();
  report.satisfied = report.violated_count == 0;
  report.status = report.satisfied ? "satisfied" : "violated";
  return report;
}

}  // namespace amr::planner::validator
