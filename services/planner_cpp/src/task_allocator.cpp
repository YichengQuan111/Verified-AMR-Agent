#include "task_allocator/task_allocator.hpp"

#include <algorithm>
#include <cmath>
#include <functional>
#include <limits>
#include <map>
#include <numeric>
#include <queue>
#include <set>
#include <utility>

namespace amr::planner {
namespace {

constexpr double kComparisonEpsilon = 1.0e-9;

struct NormalizedInput {
  std::vector<AMRState> amrs;
  std::vector<TransportOrder> orders;
  std::map<std::string, GridPosition> locations;
  std::set<std::string> completed_order_ids;
};

struct EvaluatedInput {
  NormalizedInput normalized;
  std::vector<std::vector<PairEvaluation>> pairs;
};

void require_finite_nonnegative(double value, const char* field) {
  if (!std::isfinite(value) || value < 0.0) {
    throw AllocationError("invalid_request", std::string(field) + " must be finite and non-negative");
  }
}

void require_position(const GridPosition& position, const char* field) {
  if (position.x < 0 || position.x >= 30 || position.y < 0 || position.y >= 20) {
    throw AllocationError("invalid_request", std::string(field) + " must be inside the 30x20 grid");
  }
}

bool is_valid_heading(int heading) {
  return heading == 0 || heading == 90 || heading == 180 || heading == 270;
}

void validate_unique_nonempty(const std::vector<std::string>& ids, const char* field) {
  std::set<std::string> unique_ids;
  for (const auto& id : ids) {
    if (id.empty()) {
      throw AllocationError("invalid_request", std::string(field) + " contains an empty ID");
    }
    if (!unique_ids.insert(id).second) {
      throw AllocationError("invalid_request", std::string(field) + " contains duplicate ID: " + id);
    }
  }
}

void validate_request(const AllocationRequest& request) {
  if (request.amrs.empty()) {
    throw AllocationError("invalid_request", "amrs must contain at least one AMR");
  }
  if (request.orders.empty()) {
    throw AllocationError("invalid_request", "orders must contain at least one order");
  }

  std::vector<std::string> amr_ids;
  amr_ids.reserve(request.amrs.size());
  for (const auto& amr : request.amrs) {
    amr_ids.push_back(amr.amr_id);
    require_position(amr.position, "AMR position");
    if (!is_valid_heading(amr.heading)) {
      throw AllocationError("invalid_request", "AMR heading must be 0, 90, 180, or 270");
    }
    if (!std::isfinite(amr.battery) || amr.battery < 0.0 || amr.battery > 100.0) {
      throw AllocationError("invalid_request", "AMR battery must be between 0 and 100");
    }
    require_finite_nonnegative(amr.load, "AMR load");
  }
  validate_unique_nonempty(amr_ids, "amrs");

  std::vector<std::string> order_ids;
  order_ids.reserve(request.orders.size());
  std::set<std::string> known_orders;
  for (const auto& order : request.orders) {
    order_ids.push_back(order.order_id);
    if (order.order_id.empty() || order.material_id.empty() || order.pickup.empty() ||
        order.dropoff.empty()) {
      throw AllocationError("invalid_request", "order IDs, material_id, pickup, and dropoff are required");
    }
    if (order.priority < 1 || order.priority > 5) {
      throw AllocationError("invalid_request", "order priority must be between 1 and 5");
    }
    if (order.release_time < 0 || order.deadline <= order.release_time) {
      throw AllocationError("invalid_request", "order deadline must be later than release_time");
    }
    if (order.pickup == order.dropoff) {
      throw AllocationError("invalid_request", "order pickup and dropoff must be different");
    }
    std::set<std::string> dependencies(order.dependencies.begin(), order.dependencies.end());
    if (dependencies.size() != order.dependencies.size()) {
      throw AllocationError("invalid_request", "order dependencies must not contain duplicates");
    }
    if (std::find(order.dependencies.begin(), order.dependencies.end(), order.order_id) !=
        order.dependencies.end()) {
      throw AllocationError("invalid_request", "an order cannot depend on itself: " + order.order_id);
    }
    known_orders.insert(order.order_id);
  }
  validate_unique_nonempty(order_ids, "orders");
  for (const auto& order : request.orders) {
    for (const auto& dependency : order.dependencies) {
      if (known_orders.find(dependency) == known_orders.end()) {
        throw AllocationError("invalid_request", "order dependency is unknown: " + dependency);
      }
    }
  }
  validate_unique_nonempty(request.completed_order_ids, "completed_order_ids");

  // P0-04 的 TaskContract 使用 Kahn 算法拒绝订单依赖环；C++ 入口也必须复现
  // 这个门禁，否则直接调用 CLI 会把一个永远无法执行的订单图交给分配器。
  std::map<std::string, int> indegree;
  std::map<std::string, std::vector<std::string>> dependents;
  for (const auto& order : request.orders) {
    indegree.emplace(order.order_id, 0);
    dependents.emplace(order.order_id, std::vector<std::string>{});
  }
  for (const auto& order : request.orders) {
    indegree[order.order_id] = static_cast<int>(order.dependencies.size());
    for (const auto& dependency : order.dependencies) {
      dependents[dependency].push_back(order.order_id);
    }
  }
  std::priority_queue<std::string, std::vector<std::string>, std::greater<std::string>> ready;
  for (const auto& entry : indegree) {
    if (entry.second == 0) {
      ready.push(entry.first);
    }
  }
  std::size_t visited = 0;
  while (!ready.empty()) {
    const std::string current = ready.top();
    ready.pop();
    ++visited;
    for (const auto& dependent : dependents[current]) {
      if (--indegree[dependent] == 0) {
        ready.push(dependent);
      }
    }
  }
  if (visited != request.orders.size()) {
    throw AllocationError("invalid_request", "order dependencies contain a cycle");
  }

  std::vector<std::string> location_ids;
  location_ids.reserve(request.locations.size());
  for (const auto& location : request.locations) {
    location_ids.push_back(location.location_id);
    require_position(location.position, "location position");
  }
  validate_unique_nonempty(location_ids, "locations");

  if (request.config.current_time < 0) {
    throw AllocationError("invalid_request", "config.current_time must be non-negative");
  }
  require_finite_nonnegative(request.config.maximum_load_kg, "config.maximum_load_kg");
  require_finite_nonnegative(
      request.config.travel_speed_cells_per_second, "config.travel_speed_cells_per_second");
  require_finite_nonnegative(
      request.config.energy_per_cell_percent, "config.energy_per_cell_percent");
  if (request.config.maximum_load_kg <= 0.0 ||
      request.config.travel_speed_cells_per_second <= 0.0) {
    throw AllocationError("invalid_request", "maximum_load_kg and travel speed must be positive");
  }
  const auto check_percentage = [](double value, const char* field) {
    if (!std::isfinite(value) || value < 0.0 || value > 100.0) {
      throw AllocationError("invalid_request", std::string(field) + " must be between 0 and 100");
    }
  };
  check_percentage(request.config.battery_warning_threshold_percent, "battery warning threshold");
  check_percentage(request.config.new_task_battery_threshold_percent, "new task battery threshold");
  check_percentage(request.config.critical_battery_threshold_percent, "critical battery threshold");
  check_percentage(request.config.battery_safety_reserve_percent, "battery safety reserve");
  if (request.config.critical_battery_threshold_percent >
          request.config.new_task_battery_threshold_percent ||
      request.config.new_task_battery_threshold_percent >
          request.config.battery_warning_threshold_percent ||
      request.config.critical_battery_threshold_percent >
          request.config.battery_safety_reserve_percent) {
    throw AllocationError("invalid_request", "battery thresholds are not ordered consistently");
  }

  const auto check_weight = [](double value, const char* field) {
    if (!std::isfinite(value) || value < 0.0 || value > 1.0e8) {
      throw AllocationError("invalid_request", std::string(field) + " must be finite and non-negative");
    }
  };
  check_weight(request.weights.distance, "weights.distance");
  check_weight(request.weights.lateness_risk, "weights.lateness_risk");
  check_weight(request.weights.battery_risk, "weights.battery_risk");
  check_weight(request.weights.load_penalty, "weights.load_penalty");
  check_weight(request.weights.priority_bonus, "weights.priority_bonus");
}

NormalizedInput normalize(const AllocationRequest& request) {
  validate_request(request);
  NormalizedInput normalized;
  normalized.amrs = request.amrs;
  normalized.orders = request.orders;
  std::sort(normalized.amrs.begin(), normalized.amrs.end(),
            [](const AMRState& left, const AMRState& right) { return left.amr_id < right.amr_id; });
  std::sort(normalized.orders.begin(), normalized.orders.end(),
            [](const TransportOrder& left, const TransportOrder& right) {
              return left.order_id < right.order_id;
            });
  for (const auto& location : request.locations) {
    normalized.locations.emplace(location.location_id, location.position);
  }
  normalized.completed_order_ids.insert(
      request.completed_order_ids.begin(), request.completed_order_ids.end());
  return normalized;
}

int manhattan_distance(const GridPosition& left, const GridPosition& right) {
  return std::abs(left.x - right.x) + std::abs(left.y - right.y);
}

void add_reason(PairEvaluation& evaluation, const char* code, const char* reason) {
  evaluation.reason_codes.emplace_back(code);
  evaluation.reasons.emplace_back(reason);
}

PairEvaluation evaluate_pair(const AMRState& amr, const TransportOrder& order,
                             const AllocationRequest& request, const NormalizedInput& normalized) {
  PairEvaluation evaluation;
  const auto pickup = normalized.locations.find(order.pickup);
  const auto dropoff = normalized.locations.find(order.dropoff);
  if (pickup == normalized.locations.end()) {
    add_reason(evaluation, "pickup_location_missing", "The order pickup location is missing from the snapshot.");
  }
  if (dropoff == normalized.locations.end()) {
    add_reason(evaluation, "dropoff_location_missing", "The order dropoff location is missing from the snapshot.");
  }
  for (const auto& dependency : order.dependencies) {
    if (normalized.completed_order_ids.find(dependency) ==
        normalized.completed_order_ids.end()) {
      add_reason(evaluation, "order_dependency_pending", "A required order dependency is not completed.");
      break;
    }
  }
  if (amr.task_status != AMRTaskStatus::kIdle) {
    add_reason(evaluation, "amr_not_idle", "The AMR is not in IDLE state.");
  }
  if (amr.health_status != HealthStatus::kHealthy) {
    add_reason(evaluation, "amr_not_healthy", "The AMR health status is not HEALTHY.");
  }
  if (amr.connection_status != ConnectionStatus::kOnline) {
    add_reason(evaluation, "amr_not_online", "The AMR connection status is not ONLINE.");
  }
  if (amr.battery <= request.config.critical_battery_threshold_percent + kComparisonEpsilon) {
    add_reason(evaluation, "battery_critical", "Battery is at or below the critical threshold.");
  } else if (amr.battery <= request.config.new_task_battery_threshold_percent + kComparisonEpsilon) {
    add_reason(evaluation, "battery_below_new_task_threshold",
               "Battery is too low for a new ordinary transport order.");
  }
  if (amr.load > request.config.maximum_load_kg + kComparisonEpsilon) {
    add_reason(evaluation, "current_load_exceeds_limit", "The current AMR load exceeds the configured limit.");
  }
  if (!evaluation.reason_codes.empty() || pickup == normalized.locations.end() ||
      dropoff == normalized.locations.end()) {
    return evaluation;
  }

  const double distance_to_pickup = manhattan_distance(amr.position, pickup->second);
  const double pickup_to_dropoff = manhattan_distance(pickup->second, dropoff->second);
  const double route_distance = distance_to_pickup + pickup_to_dropoff;
  const double travel_time = std::ceil(
      route_distance / request.config.travel_speed_cells_per_second);
  const double estimated_start = std::max(request.config.current_time, order.release_time);
  const double estimated_completion = estimated_start + travel_time;
  const double lateness_seconds = std::max(0.0, estimated_completion - order.deadline);
  const double deadline_span = std::max(1, order.deadline - order.release_time);
  const double lateness_risk = lateness_seconds / deadline_span;
  const double estimated_battery_after =
      amr.battery - route_distance * request.config.energy_per_cell_percent;
  if (estimated_battery_after + kComparisonEpsilon <
      request.config.battery_safety_reserve_percent) {
    add_reason(evaluation, "completion_below_safety_reserve",
               "Estimated battery after order completion is below the safety reserve.");
    return evaluation;
  }

  const double warning_span = std::max(
      1.0, request.config.battery_warning_threshold_percent -
               request.config.new_task_battery_threshold_percent);
  const double battery_risk =
      std::max(0.0, (request.config.battery_warning_threshold_percent - amr.battery) /
                           warning_span) +
      std::max(0.0, request.config.battery_safety_reserve_percent - estimated_battery_after) /
          std::max(1.0, request.config.battery_safety_reserve_percent);
  const double load_penalty = amr.load / request.config.maximum_load_kg;
  const double priority_bonus = static_cast<double>(order.priority) / 5.0;
  const double total_cost =
      request.weights.distance * distance_to_pickup +
      request.weights.lateness_risk * lateness_risk +
      request.weights.battery_risk * battery_risk +
      request.weights.load_penalty * load_penalty -
      request.weights.priority_bonus * priority_bonus;
  if (!std::isfinite(total_cost) || std::abs(total_cost) >= kInternalInf / 16.0) {
    throw AllocationError("invalid_request", "cost calculation exceeded the safe numeric range");
  }

  evaluation.feasible = true;
  evaluation.cost = total_cost;
  evaluation.components = CostBreakdown{
      distance_to_pickup,
      route_distance,
      estimated_completion,
      lateness_risk,
      priority_bonus,
      battery_risk,
      estimated_battery_after,
      load_penalty,
      total_cost,
  };
  return evaluation;
}

EvaluatedInput evaluate_request(const AllocationRequest& request) {
  EvaluatedInput evaluated;
  evaluated.normalized = normalize(request);
  evaluated.pairs.resize(evaluated.normalized.amrs.size());
  for (std::size_t amr_index = 0; amr_index < evaluated.normalized.amrs.size(); ++amr_index) {
    auto& row = evaluated.pairs[amr_index];
    row.reserve(evaluated.normalized.orders.size());
    for (const auto& order : evaluated.normalized.orders) {
      row.push_back(evaluate_pair(evaluated.normalized.amrs[amr_index], order, request,
                                  evaluated.normalized));
    }
  }
  return evaluated;
}

std::vector<int> hungarian_minimize(const std::vector<std::vector<double>>& costs) {
  const std::size_t row_count = costs.size();
  if (row_count == 0) {
    return {};
  }
  const std::size_t column_count = costs.front().size();
  if (column_count < row_count) {
    throw AllocationError("internal_error", "Hungarian matrix must have at least as many columns as rows");
  }
  for (const auto& row : costs) {
    if (row.size() != column_count) {
      throw AllocationError("internal_error", "Hungarian matrix rows have inconsistent sizes");
    }
  }

  // 这是 cp-algorithms 形式的势函数实现，u/v 使每轮增广只处理尚未匹配的
  // 列。dummy 选项保证每一行至少有有限代价，从而 INF 不会触发未定义增广。
  std::vector<double> u(row_count + 1, 0.0);
  std::vector<double> v(column_count + 1, 0.0);
  std::vector<int> p(column_count + 1, 0);
  std::vector<int> way(column_count + 1, 0);

  for (std::size_t row = 1; row <= row_count; ++row) {
    p[0] = static_cast<int>(row);
    std::vector<double> min_value(column_count + 1, kInternalInf);
    std::vector<bool> used(column_count + 1, false);
    std::size_t column = 0;
    do {
      used[column] = true;
      const int current_row = p[column];
      double delta = kInternalInf;
      std::size_t next_column = 0;
      for (std::size_t candidate = 1; candidate <= column_count; ++candidate) {
        if (used[candidate]) {
          continue;
        }
        const double reduced_cost = costs[static_cast<std::size_t>(current_row - 1)][candidate - 1] -
                                    u[static_cast<std::size_t>(current_row)] - v[candidate];
        if (reduced_cost < min_value[candidate]) {
          min_value[candidate] = reduced_cost;
          way[candidate] = static_cast<int>(column);
        }
        if (min_value[candidate] < delta ||
            (std::abs(min_value[candidate] - delta) <= kComparisonEpsilon &&
             candidate < next_column)) {
          delta = min_value[candidate];
          next_column = candidate;
        }
      }
      if (next_column == 0 || delta >= kInternalInf / 2.0) {
        throw AllocationError("internal_error", "Hungarian failed to find a finite augmenting path");
      }
      for (std::size_t candidate = 0; candidate <= column_count; ++candidate) {
        if (used[candidate]) {
          u[static_cast<std::size_t>(p[candidate])] += delta;
          v[candidate] -= delta;
        } else {
          min_value[candidate] -= delta;
        }
      }
      column = next_column;
    } while (p[column] != 0);

    do {
      const std::size_t previous_column = static_cast<std::size_t>(way[column]);
      p[column] = p[previous_column];
      column = previous_column;
    } while (column != 0);
  }

  std::vector<int> assignment(row_count, -1);
  for (std::size_t column = 1; column <= column_count; ++column) {
    if (p[column] != 0) {
      assignment[static_cast<std::size_t>(p[column] - 1)] = static_cast<int>(column - 1);
    }
  }
  return assignment;
}

double choose_unassigned_penalty(const std::vector<std::vector<PairEvaluation>>& pairs) {
  double largest_absolute_cost = 1.0;
  for (const auto& row : pairs) {
    for (const auto& pair : row) {
      if (pair.feasible) {
        largest_absolute_cost = std::max(largest_absolute_cost, std::abs(pair.cost));
      }
    }
  }
  // 每个真实匹配最多替代一对 dummy 匹配；4 倍余量确保优先最大化可行匹配数，
  // 同时仍远小于 kInternalInf，避免把合法代价和 INF 混淆。
  return (largest_absolute_cost + 1.0) * 4.0;
}

std::vector<int> solve_with_dummies(const std::vector<std::vector<PairEvaluation>>& pairs) {
  const std::size_t amr_count = pairs.size();
  const std::size_t order_count = pairs.front().size();
  const std::size_t dimension = amr_count + order_count;
  const double unassigned_penalty = choose_unassigned_penalty(pairs);
  std::vector<std::vector<double>> costs(dimension, std::vector<double>(dimension, 0.0));
  for (std::size_t amr = 0; amr < amr_count; ++amr) {
    for (std::size_t order = 0; order < order_count; ++order) {
      if (pairs[amr][order].feasible) {
        // 微小稳定扰动只参与决策，不写入返回的业务 cost。
        const double tie_break =
            1.0e-9 * static_cast<double>(1 + amr * order_count + order);
        costs[amr][order] = pairs[amr][order].cost + tie_break;
      } else {
        costs[amr][order] = kInternalInf;
      }
    }
    for (std::size_t dummy_order = 0; dummy_order < amr_count; ++dummy_order) {
      costs[amr][order_count + dummy_order] =
          unassigned_penalty + 1.0e-9 * static_cast<double>(dummy_order + 1);
    }
  }
  for (std::size_t dummy_amr = 0; dummy_amr < order_count; ++dummy_amr) {
    for (std::size_t order = 0; order < order_count; ++order) {
      costs[amr_count + dummy_amr][order] =
          unassigned_penalty + 1.0e-9 * static_cast<double>(order + 1);
    }
    for (std::size_t dummy_order = 0; dummy_order < amr_count; ++dummy_order) {
      costs[amr_count + dummy_amr][order_count + dummy_order] = 0.0;
    }
  }
  return hungarian_minimize(costs);
}

std::vector<CandidateReason> candidate_reasons_for_order(
    std::size_t order_index, const EvaluatedInput& evaluated) {
  std::vector<CandidateReason> candidates;
  candidates.reserve(evaluated.normalized.amrs.size());
  for (std::size_t amr_index = 0; amr_index < evaluated.normalized.amrs.size(); ++amr_index) {
    const auto& pair = evaluated.pairs[amr_index][order_index];
    candidates.push_back(CandidateReason{
        evaluated.normalized.amrs[amr_index].amr_id,
        pair.reason_codes,
        pair.reasons,
    });
  }
  return candidates;
}

AllocationResult make_result_base(const std::string& algorithm, const EvaluatedInput& evaluated) {
  AllocationResult result;
  result.algorithm = algorithm;
  result.amr_ids.reserve(evaluated.normalized.amrs.size());
  result.order_ids.reserve(evaluated.normalized.orders.size());
  for (const auto& amr : evaluated.normalized.amrs) {
    result.amr_ids.push_back(amr.amr_id);
  }
  for (const auto& order : evaluated.normalized.orders) {
    result.order_ids.push_back(order.order_id);
  }
  result.pair_evaluations = evaluated.pairs;
  return result;
}

void finish_result_status(AllocationResult& result, const EvaluatedInput& evaluated,
                          const std::set<std::string>& assigned_amrs,
                          const std::set<std::string>& assigned_orders) {
  for (const auto& amr : evaluated.normalized.amrs) {
    if (assigned_amrs.find(amr.amr_id) == assigned_amrs.end()) {
      result.unassigned_amrs.push_back(amr.amr_id);
    }
  }
  for (std::size_t order_index = 0; order_index < evaluated.normalized.orders.size(); ++order_index) {
    const auto& order = evaluated.normalized.orders[order_index];
    if (assigned_orders.find(order.order_id) != assigned_orders.end()) {
      continue;
    }
    bool has_feasible_candidate = false;
    for (const auto& row : evaluated.pairs) {
      if (row[order_index].feasible) {
        has_feasible_candidate = true;
        break;
      }
    }
    const std::string reason_code = has_feasible_candidate ? "capacity_exhausted" : "no_feasible_amr";
    result.unassigned_orders.push_back(UnassignedOrder{
        order.order_id,
        reason_code,
        {reason_code},
        candidate_reasons_for_order(order_index, evaluated),
    });
  }
  if (result.assignments.size() == evaluated.normalized.orders.size()) {
    result.status = "complete";
  } else if (result.assignments.empty()) {
    result.status = "no_feasible_assignment";
  } else {
    result.status = "partial";
  }
}

}  // namespace

AllocationError::AllocationError(std::string code, std::string message)
    : std::runtime_error(std::move(message)), code_(std::move(code)) {}

AllocationResult allocate_hungarian(const AllocationRequest& request) {
  const EvaluatedInput evaluated = evaluate_request(request);
  AllocationResult result = make_result_base("hungarian", evaluated);
  const std::vector<int> assignment = solve_with_dummies(evaluated.pairs);
  std::set<std::string> assigned_amrs;
  std::set<std::string> assigned_orders;
  for (std::size_t amr_index = 0; amr_index < evaluated.normalized.amrs.size(); ++amr_index) {
    const int column = assignment[amr_index];
    if (column < 0 || static_cast<std::size_t>(column) >= evaluated.normalized.orders.size()) {
      continue;
    }
    const auto& pair = evaluated.pairs[amr_index][static_cast<std::size_t>(column)];
    if (!pair.feasible || !pair.components.has_value()) {
      continue;
    }
    const auto& amr = evaluated.normalized.amrs[amr_index];
    const auto& order = evaluated.normalized.orders[static_cast<std::size_t>(column)];
    result.assignments.push_back(Assignment{amr.amr_id, order.order_id, *pair.components});
    result.total_cost += pair.cost;
    assigned_amrs.insert(amr.amr_id);
    assigned_orders.insert(order.order_id);
  }
  std::sort(result.assignments.begin(), result.assignments.end(),
            [](const Assignment& left, const Assignment& right) {
              return left.order_id < right.order_id;
            });
  finish_result_status(result, evaluated, assigned_amrs, assigned_orders);
  return result;
}

AllocationResult allocate_nearest_idle(const AllocationRequest& request) {
  const EvaluatedInput evaluated = evaluate_request(request);
  AllocationResult result = make_result_base("nearest_idle_amr", evaluated);
  std::set<std::string> assigned_amrs;
  std::set<std::string> assigned_orders;

  // 基线只比较 pickup 距离；不使用 Hungarian 的 dummy 矩阵、权重或总成本。
  for (std::size_t order_index = 0; order_index < evaluated.normalized.orders.size(); ++order_index) {
    std::size_t best_amr = evaluated.normalized.amrs.size();
    for (std::size_t amr_index = 0; amr_index < evaluated.normalized.amrs.size(); ++amr_index) {
      const auto& amr = evaluated.normalized.amrs[amr_index];
      const auto& pair = evaluated.pairs[amr_index][order_index];
      if (!pair.feasible || assigned_amrs.find(amr.amr_id) != assigned_amrs.end()) {
        continue;
      }
      if (best_amr == evaluated.normalized.amrs.size() ||
          pair.components->distance_to_pickup <
              evaluated.pairs[best_amr][order_index].components->distance_to_pickup ||
          (std::abs(pair.components->distance_to_pickup -
                    evaluated.pairs[best_amr][order_index].components->distance_to_pickup) <=
               kComparisonEpsilon &&
           amr.amr_id < evaluated.normalized.amrs[best_amr].amr_id)) {
        best_amr = amr_index;
      }
    }
    if (best_amr == evaluated.normalized.amrs.size()) {
      continue;
    }
    const auto& amr = evaluated.normalized.amrs[best_amr];
    const auto& order = evaluated.normalized.orders[order_index];
    const auto& pair = evaluated.pairs[best_amr][order_index];
    result.assignments.push_back(Assignment{amr.amr_id, order.order_id, *pair.components});
    result.total_cost += pair.cost;
    assigned_amrs.insert(amr.amr_id);
    assigned_orders.insert(order.order_id);
  }
  std::sort(result.assignments.begin(), result.assignments.end(),
            [](const Assignment& left, const Assignment& right) {
              return left.order_id < right.order_id;
            });
  finish_result_status(result, evaluated, assigned_amrs, assigned_orders);
  return result;
}

}  // namespace amr::planner
