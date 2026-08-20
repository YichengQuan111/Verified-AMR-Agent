#include "route_planner/json_codec.hpp"

#include <cmath>
#include <limits>
#include <set>
#include <string_view>
#include <utility>

namespace amr::planner::route_json {
namespace {

using Object = json::Value::Object;

const json::Value& required_field(const Object& object, const std::string& name) {
  const auto it = object.find(name);
  if (it == object.end()) throw json::ParseError("missing field: " + name);
  return it->second;
}

void require_exact_keys(const Object& object,
                        std::initializer_list<std::string_view> expected,
                        const std::string& context) {
  std::set<std::string> expected_keys;
  for (const auto key : expected) expected_keys.emplace(key);
  if (object.size() != expected_keys.size()) {
    throw json::ParseError(context + " contains unknown or missing fields");
  }
  for (const auto& entry : object) {
    if (expected_keys.count(entry.first) == 0U) {
      throw json::ParseError(context + " contains unknown field: " + entry.first);
    }
  }
}

std::string string_field(const Object& object, const std::string& name) {
  const auto& value = required_field(object, name);
  if (!value.is_string()) throw json::ParseError(name + " must be a string");
  return value.as_string();
}

int integer_field(const Object& object, const std::string& name) {
  const auto& value = required_field(object, name);
  if (!value.is_number() || !std::isfinite(value.as_number()) ||
      std::trunc(value.as_number()) != value.as_number() ||
      value.as_number() < static_cast<double>(std::numeric_limits<int>::min()) ||
      value.as_number() > static_cast<double>(std::numeric_limits<int>::max())) {
    throw json::ParseError(name + " must be a finite integer");
  }
  return static_cast<int>(value.as_number());
}

double number_field(const Object& object, const std::string& name) {
  const auto& value = required_field(object, name);
  if (!value.is_number() || !std::isfinite(value.as_number())) {
    throw json::ParseError(name + " must be a finite number");
  }
  return value.as_number();
}

const Object& object_field(const Object& object, const std::string& name) {
  const auto& value = required_field(object, name);
  if (!value.is_object()) throw json::ParseError(name + " must be an object");
  return value.as_object();
}

const json::Value::Array& array_field(const Object& object, const std::string& name) {
  const auto& value = required_field(object, name);
  if (!value.is_array()) throw json::ParseError(name + " must be an array");
  return value.as_array();
}

GridPosition position_value(const json::Value& value, const std::string& context) {
  if (!value.is_object()) throw json::ParseError(context + " must be an object");
  const auto& object = value.as_object();
  require_exact_keys(object, {"x", "y"}, context);
  return GridPosition{integer_field(object, "x"), integer_field(object, "y")};
}

RouteEdge edge_value(const json::Value& value, const std::string& context) {
  if (!value.is_object()) throw json::ParseError(context + " must be an object");
  const auto& object = value.as_object();
  require_exact_keys(object, {"from", "to"}, context);
  return RouteEdge{
      position_value(required_field(object, "from"), context + ".from"),
      position_value(required_field(object, "to"), context + ".to"),
  };
}

std::vector<GridPosition> positions_array(const Object& object, const std::string& name) {
  std::vector<GridPosition> positions;
  for (const auto& value : array_field(object, name)) {
    positions.push_back(position_value(value, name + " item"));
  }
  return positions;
}

std::vector<RouteEdge> edges_array(const Object& object, const std::string& name) {
  std::vector<RouteEdge> edges;
  for (const auto& value : array_field(object, name)) {
    edges.push_back(edge_value(value, name + " item"));
  }
  return edges;
}

std::vector<std::string> strings_array(const Object& object, const std::string& name) {
  std::vector<std::string> values;
  for (const auto& value : array_field(object, name)) {
    if (!value.is_string()) throw json::ParseError(name + " items must be strings");
    values.push_back(value.as_string());
  }
  return values;
}

AMRTaskStatus task_status_value(const std::string& value) {
  if (value == "IDLE") return AMRTaskStatus::kIdle;
  if (value == "TO_PICKUP") return AMRTaskStatus::kToPickup;
  if (value == "LOADING") return AMRTaskStatus::kLoading;
  if (value == "TO_DROPOFF") return AMRTaskStatus::kToDropoff;
  if (value == "UNLOADING") return AMRTaskStatus::kUnloading;
  if (value == "TO_CHARGE") return AMRTaskStatus::kToCharge;
  if (value == "CHARGING") return AMRTaskStatus::kCharging;
  if (value == "OFFLINE") return AMRTaskStatus::kOffline;
  throw json::ParseError("invalid AMR task_status");
}

HealthStatus health_status_value(const std::string& value) {
  if (value == "HEALTHY") return HealthStatus::kHealthy;
  if (value == "DEGRADED") return HealthStatus::kDegraded;
  if (value == "FAULT") return HealthStatus::kFault;
  throw json::ParseError("invalid AMR health_status");
}

ConnectionStatus connection_status_value(const std::string& value) {
  if (value == "ONLINE") return ConnectionStatus::kOnline;
  if (value == "DEGRADED") return ConnectionStatus::kDegraded;
  if (value == "OFFLINE") return ConnectionStatus::kOffline;
  throw json::ParseError("invalid AMR connection_status");
}

const char* task_status_name(AMRTaskStatus status) noexcept {
  switch (status) {
    case AMRTaskStatus::kIdle: return "IDLE";
    case AMRTaskStatus::kToPickup: return "TO_PICKUP";
    case AMRTaskStatus::kLoading: return "LOADING";
    case AMRTaskStatus::kToDropoff: return "TO_DROPOFF";
    case AMRTaskStatus::kUnloading: return "UNLOADING";
    case AMRTaskStatus::kToCharge: return "TO_CHARGE";
    case AMRTaskStatus::kCharging: return "CHARGING";
    case AMRTaskStatus::kOffline: return "OFFLINE";
  }
  return "UNKNOWN";
}

const char* health_status_name(HealthStatus status) noexcept {
  switch (status) {
    case HealthStatus::kHealthy: return "HEALTHY";
    case HealthStatus::kDegraded: return "DEGRADED";
    case HealthStatus::kFault: return "FAULT";
  }
  return "UNKNOWN";
}

const char* connection_status_name(ConnectionStatus status) noexcept {
  switch (status) {
    case ConnectionStatus::kOnline: return "ONLINE";
    case ConnectionStatus::kDegraded: return "DEGRADED";
    case ConnectionStatus::kOffline: return "OFFLINE";
  }
  return "UNKNOWN";
}

json::Value position_to_value(const GridPosition& position) {
  return json::Value::Object{
      {"x", json::Value(static_cast<double>(position.x))},
      {"y", json::Value(static_cast<double>(position.y))},
  };
}

json::Value edge_to_value(const RouteEdge& edge) {
  return json::Value::Object{
      {"from", position_to_value(edge.from)},
      {"to", position_to_value(edge.to)},
  };
}

json::Value nullable_string(const std::string& value) {
  return value.empty() ? json::Value(nullptr) : json::Value(value);
}

json::Value path_to_value(const std::vector<RouteStep>& path) {
  json::Value::Array values;
  values.reserve(path.size());
  for (const auto& step : path) {
    values.emplace_back(json::Value::Object{
        {"action", json::Value(route_action_name(step.action))},
        {"g_cost", json::Value(step.g_cost)},
        {"heading", json::Value(static_cast<double>(step.heading))},
        {"position", position_to_value(step.position)},
        {"time", json::Value(static_cast<double>(step.time))},
    });
  }
  return json::Value(std::move(values));
}

}  // namespace

RouteRequest request_from_value(const json::Value& value) {
  if (!value.is_object()) throw json::ParseError("route request must be a JSON object");
  const auto& root = value.as_object();
  require_exact_keys(root,
                     {"schema_version", "environment_ref", "map_width", "map_height",
                      "blocked_cells", "blocked_edges", "one_way_edges", "amrs", "orders",
                      "location_positions", "assignments", "completed_order_ids", "start_time",
                      "max_time", "costs"},
                     "route request");
  if (string_field(root, "schema_version") != "1.0") {
    throw json::ParseError("schema_version must be \"1.0\"");
  }

  RouteRequest request;
  request.environment_ref = string_field(root, "environment_ref");
  request.map.width = integer_field(root, "map_width");
  request.map.height = integer_field(root, "map_height");
  request.map.blocked_cells = positions_array(root, "blocked_cells");
  request.map.blocked_edges = edges_array(root, "blocked_edges");
  request.map.one_way_edges = edges_array(root, "one_way_edges");
  request.start_time = integer_field(root, "start_time");
  request.max_time = integer_field(root, "max_time");
  request.completed_order_ids = strings_array(root, "completed_order_ids");

  for (const auto& value_item : array_field(root, "amrs")) {
    if (!value_item.is_object()) throw json::ParseError("each AMR must be an object");
    const auto& object = value_item.as_object();
    require_exact_keys(object,
                       {"amr_id", "position", "heading", "battery", "load",
                        "task_status", "health_status", "connection_status"},
                       "AMR");
    request.amrs.push_back(AMRState{
        string_field(object, "amr_id"),
        position_value(required_field(object, "position"), "AMR position"),
        integer_field(object, "heading"),
        number_field(object, "battery"),
        number_field(object, "load"),
        task_status_value(string_field(object, "task_status")),
        health_status_value(string_field(object, "health_status")),
        connection_status_value(string_field(object, "connection_status")),
    });
  }

  for (const auto& value_item : array_field(root, "orders")) {
    if (!value_item.is_object()) throw json::ParseError("each order must be an object");
    const auto& object = value_item.as_object();
    require_exact_keys(object,
                       {"order_id", "material_id", "pickup", "dropoff", "priority",
                        "release_time", "deadline", "dependencies"},
                       "transport order");
    request.orders.push_back(TransportOrder{
        string_field(object, "order_id"),
        string_field(object, "material_id"),
        string_field(object, "pickup"),
        string_field(object, "dropoff"),
        integer_field(object, "priority"),
        integer_field(object, "release_time"),
        integer_field(object, "deadline"),
        strings_array(object, "dependencies"),
    });
  }

  const auto& locations = object_field(root, "location_positions");
  for (const auto& entry : locations) {
    if (entry.first.empty()) throw json::ParseError("location ID must not be empty");
    request.locations.push_back(Location{
        entry.first,
        position_value(entry.second, "location position"),
    });
  }

  for (const auto& value_item : array_field(root, "assignments")) {
    if (!value_item.is_object()) throw json::ParseError("each assignment must be an object");
    const auto& object = value_item.as_object();
    // 允许直接使用 P0-08 AllocationResult.assignments；components 只作为上游
    // 审计快照被校验为对象，路线规划会重新按真实地图和预约约束计算，绝不
    // 把旧的 Manhattan 代价当成已验证路线。
    if (object.size() == 2U) {
      require_exact_keys(object, {"amr_id", "order_id"}, "route assignment");
    } else if (object.size() == 3U && object.find("components") != object.end() &&
               object.at("components").is_object()) {
      if (object.find("amr_id") == object.end() || object.find("order_id") == object.end()) {
        throw json::ParseError("route assignment must contain amr_id and order_id");
      }
    } else {
      throw json::ParseError("route assignment contains unknown or missing fields");
    }
    request.assignments.push_back(RouteAssignment{
        string_field(object, "amr_id"),
        string_field(object, "order_id"),
    });
  }

  const auto& costs = object_field(root, "costs");
  require_exact_keys(costs, {"move_cost", "turn_cost", "wait_cost"}, "route costs");
  request.costs = RouteCostConfig{
      number_field(costs, "move_cost"),
      number_field(costs, "turn_cost"),
      number_field(costs, "wait_cost"),
  };
  return request;
}

json::Value result_to_value(const RoutePlanResult& result) {
  json::Value::Array routes;
  routes.reserve(result.routes.size());
  for (const auto& route : result.routes) {
    routes.emplace_back(json::Value::Object{
        {"amr_id", json::Value(route.amr_id)},
        {"dropoff_time", json::Value(static_cast<double>(route.dropoff_time))},
        {"expanded_states", json::Value(static_cast<double>(route.expanded_states))},
        {"order_id", json::Value(route.order_id)},
        {"path", path_to_value(route.path)},
        {"pickup_time", json::Value(static_cast<double>(route.pickup_time))},
        {"priority", json::Value(static_cast<double>(route.priority))},
        {"reason", nullable_string(route.reason)},
        {"reason_code", nullable_string(route.reason_code)},
        {"status", json::Value(route.status)},
        {"total_cost", json::Value(route.total_cost)},
    });
  }
  return json::Value::Object{
      {"algorithm", json::Value(result.algorithm)},
      {"cell_reservation_count", json::Value(static_cast<double>(result.cell_reservation_count))},
      {"edge_reservation_count", json::Value(static_cast<double>(result.edge_reservation_count))},
      {"planned_count", json::Value(static_cast<double>(result.planned_count))},
      {"routes", json::Value(std::move(routes))},
      {"schema_version", json::Value("1.0")},
      {"status", json::Value(result.status)},
      {"total_cost", json::Value(result.total_cost)},
      {"total_expanded_states", json::Value(static_cast<double>(result.total_expanded_states))},
  };
}

json::Value error_to_value(const std::string& code, const std::string& message) {
  return json::Value::Object{
      {"error", json::Value::Object{
                     {"code", json::Value(code)},
                     {"message", json::Value(message)},
                 }},
      {"schema_version", json::Value("1.0")},
      {"status", json::Value("error")},
  };
}

}  // namespace amr::planner::route_json
