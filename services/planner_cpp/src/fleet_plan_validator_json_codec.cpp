#include "fleet_plan_validator/json_codec.hpp"

#include <cmath>
#include <initializer_list>
#include <limits>
#include <set>
#include <string_view>
#include <utility>

namespace amr::planner::validator_json {
namespace {

using Object = json::Value::Object;
using Array = json::Value::Array;

const json::Value& required_field(const Object& object, const std::string& name) {
  const auto it = object.find(name);
  if (it == object.end()) throw json::ParseError("missing field: " + name);
  return it->second;
}

void require_keys(const Object& object,
                  std::initializer_list<std::string_view> required,
                  std::initializer_list<std::string_view> optional,
                  const std::string& context) {
  std::set<std::string> required_keys;
  std::set<std::string> optional_keys;
  for (const auto key : required) required_keys.emplace(key);
  for (const auto key : optional) optional_keys.emplace(key);
  for (const auto& entry : object) {
    if (required_keys.count(entry.first) == 0U && optional_keys.count(entry.first) == 0U) {
      throw json::ParseError(context + " contains unknown field: " + entry.first);
    }
  }
  for (const auto& key : required_keys) {
    if (object.find(key) == object.end()) {
      throw json::ParseError(context + " missing field: " + key);
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

void optional_integer_field(const Object& object, const std::string& name) {
  const auto it = object.find(name);
  if (it == object.end()) return;
  if (!it->second.is_number() || !std::isfinite(it->second.as_number()) ||
      std::trunc(it->second.as_number()) != it->second.as_number() ||
      it->second.as_number() < static_cast<double>(std::numeric_limits<int>::min()) ||
      it->second.as_number() > static_cast<double>(std::numeric_limits<int>::max())) {
    throw json::ParseError(name + " must be a finite integer");
  }
}

void optional_number_field(const Object& object, const std::string& name) {
  const auto it = object.find(name);
  if (it == object.end()) return;
  if (!it->second.is_number() || !std::isfinite(it->second.as_number())) {
    throw json::ParseError(name + " must be a finite number");
  }
}

const Object& object_field(const Object& object, const std::string& name) {
  const auto& value = required_field(object, name);
  if (!value.is_object()) throw json::ParseError(name + " must be an object");
  return value.as_object();
}

const Array& array_field(const Object& object, const std::string& name) {
  const auto& value = required_field(object, name);
  if (!value.is_array()) throw json::ParseError(name + " must be an array");
  return value.as_array();
}

GridPosition position_value(const json::Value& value, const std::string& context) {
  if (!value.is_object()) throw json::ParseError(context + " must be an object");
  const auto& object = value.as_object();
  require_keys(object, {"x", "y"}, {}, context);
  return GridPosition{integer_field(object, "x"), integer_field(object, "y")};
}

RouteEdge edge_value(const json::Value& value, const std::string& context) {
  if (!value.is_object()) throw json::ParseError(context + " must be an object");
  const auto& object = value.as_object();
  require_keys(object, {"from", "to"}, {}, context);
  return RouteEdge{position_value(required_field(object, "from"), context + ".from"),
                   position_value(required_field(object, "to"), context + ".to")};
}

std::vector<GridPosition> positions_array(const Object& object, const std::string& name) {
  std::vector<GridPosition> values;
  for (const auto& value : array_field(object, name)) {
    values.push_back(position_value(value, name + " item"));
  }
  return values;
}

std::vector<RouteEdge> edges_array(const Object& object, const std::string& name) {
  std::vector<RouteEdge> values;
  for (const auto& value : array_field(object, name)) {
    values.push_back(edge_value(value, name + " item"));
  }
  return values;
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

RouteAction route_action_value(const std::string& value) {
  if (value == "start") return RouteAction::kStart;
  if (value == "move") return RouteAction::kMove;
  if (value == "turn_left") return RouteAction::kTurnLeft;
  if (value == "turn_right") return RouteAction::kTurnRight;
  if (value == "wait") return RouteAction::kWait;
  throw json::ParseError("invalid route action");
}

std::string route_status_value(const Object& object) {
  const auto it = object.find("status");
  if (it == object.end()) return "planned";
  if (!it->second.is_string()) throw json::ParseError("route status must be a string");
  return it->second.as_string();
}

std::string optional_string(const Object& object, const std::string& name) {
  const auto it = object.find(name);
  if (it == object.end()) return {};
  if (!it->second.is_string()) throw json::ParseError(name + " must be a string");
  return it->second.as_string();
}

RouteStep route_step_value(const json::Value& value) {
  if (!value.is_object()) throw json::ParseError("route path item must be an object");
  const auto& object = value.as_object();
  require_keys(object, {"position", "heading", "time", "action", "g_cost"}, {},
               "route path item");
  return RouteStep{
      position_value(required_field(object, "position"), "route path position"),
      integer_field(object, "heading"),
      integer_field(object, "time"),
      route_action_value(string_field(object, "action")),
      number_field(object, "g_cost"),
  };
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

json::Value nullable_position(const std::optional<GridPosition>& value) {
  return value.has_value() ? position_to_value(*value) : json::Value(nullptr);
}

json::Value nullable_int(const std::optional<int>& value) {
  return value.has_value() ? json::Value(static_cast<double>(*value)) : json::Value(nullptr);
}

json::Value nullable_number(const std::optional<double>& value) {
  return value.has_value() ? json::Value(*value) : json::Value(nullptr);
}

const char* route_action_name_for_json(RouteAction action) noexcept {
  return route_action_name(action);
}

json::Value path_to_value(const std::vector<RouteStep>& path) {
  json::Value::Array values;
  values.reserve(path.size());
  for (const auto& step : path) {
    values.emplace_back(json::Value::Object{
        {"action", json::Value(route_action_name_for_json(step.action))},
        {"g_cost", json::Value(step.g_cost)},
        {"heading", json::Value(static_cast<double>(step.heading))},
        {"position", position_to_value(step.position)},
        {"time", json::Value(static_cast<double>(step.time))},
    });
  }
  return json::Value(std::move(values));
}

json::Value nullable_string(const std::string& value) {
  return value.empty() ? json::Value(nullptr) : json::Value(value);
}

}  // namespace

validator::FleetPlanRequest request_from_value(const json::Value& value) {
  if (!value.is_object()) throw json::ParseError("fleet plan request must be a JSON object");
  const auto& root = value.as_object();
  require_keys(root,
               {"schema_version", "environment_ref", "map_width", "map_height",
                "blocked_cells", "blocked_edges", "one_way_edges", "amrs", "orders",
                "location_positions", "completed_order_ids", "routes", "start_time",
                "max_time", "config", "workstation_capacities"},
               {"ruleset_version"}, "fleet plan request");
  if (string_field(root, "schema_version") != "1.0") {
    throw json::ParseError("schema_version must be \"1.0\"");
  }

  validator::FleetPlanRequest request;
  request.environment_ref = string_field(root, "environment_ref");
  request.map.width = integer_field(root, "map_width");
  request.map.height = integer_field(root, "map_height");
  request.map.blocked_cells = positions_array(root, "blocked_cells");
  request.map.blocked_edges = edges_array(root, "blocked_edges");
  request.map.one_way_edges = edges_array(root, "one_way_edges");
  request.completed_order_ids = strings_array(root, "completed_order_ids");
  request.start_time = integer_field(root, "start_time");
  request.max_time = integer_field(root, "max_time");
  if (root.find("ruleset_version") != root.end()) {
    request.ruleset_version = string_field(root, "ruleset_version");
  }

  for (const auto& value_item : array_field(root, "amrs")) {
    if (!value_item.is_object()) throw json::ParseError("each AMR must be an object");
    const auto& object = value_item.as_object();
    require_keys(object,
                 {"amr_id", "position", "heading", "battery", "load", "task_status",
                  "health_status", "connection_status"},
                 {}, "AMR");
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
    require_keys(object,
                 {"order_id", "material_id", "pickup", "dropoff", "priority", "release_time",
                  "deadline", "dependencies"},
                 {}, "transport order");
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

  const auto& capacities = object_field(root, "workstation_capacities");
  for (const auto& entry : capacities) {
    if (entry.first.empty()) throw json::ParseError("workstation ID must not be empty");
    if (!entry.second.is_number() || !std::isfinite(entry.second.as_number()) ||
        std::trunc(entry.second.as_number()) != entry.second.as_number() ||
        entry.second.as_number() < static_cast<double>(std::numeric_limits<int>::min()) ||
        entry.second.as_number() > static_cast<double>(std::numeric_limits<int>::max())) {
      throw json::ParseError("workstation capacity must be a finite integer");
    }
    request.workstation_capacities.emplace(entry.first,
                                           static_cast<int>(entry.second.as_number()));
  }

  for (const auto& value_item : array_field(root, "routes")) {
    if (!value_item.is_object()) throw json::ParseError("each fleet route must be an object");
    const auto& object = value_item.as_object();
    // status/reason/total_cost 等字段是 P0-09 的审计快照；它们只能被原样记录，
    // 不参与 Validator 的放行判断。禁止接受 llm_valid、skip_validation 等旁路键。
    require_keys(object,
                 {"amr_id", "order_id", "payload_kg", "pickup_time", "dropoff_time", "path"},
                 {"status", "reason_code", "reason", "priority", "total_cost",
                  "expanded_states"},
                 "fleet route");
    validator::FleetPlanRoute route;
    route.amr_id = string_field(object, "amr_id");
    route.order_id = string_field(object, "order_id");
    route.payload_kg = number_field(object, "payload_kg");
    route.pickup_time = integer_field(object, "pickup_time");
    route.dropoff_time = integer_field(object, "dropoff_time");
    route.status = route_status_value(object);
    route.planner_reason_code = optional_string(object, "reason_code");
    route.planner_reason = optional_string(object, "reason");
    optional_integer_field(object, "priority");
    optional_number_field(object, "total_cost");
    optional_integer_field(object, "expanded_states");
    for (const auto& step : array_field(object, "path")) {
      route.path.push_back(route_step_value(step));
    }
    request.routes.push_back(std::move(route));
  }

  const auto& config = object_field(root, "config");
  require_keys(config,
               {"maximum_load_kg", "energy_per_cell_percent", "battery_safety_reserve_percent",
                "new_task_battery_threshold_percent", "critical_battery_threshold_percent",
                "minimum_safety_distance_cells", "default_workstation_capacity"},
               {}, "validator config");
  request.config = validator::ValidatorConfig{
      number_field(config, "maximum_load_kg"),
      number_field(config, "energy_per_cell_percent"),
      number_field(config, "battery_safety_reserve_percent"),
      number_field(config, "new_task_battery_threshold_percent"),
      number_field(config, "critical_battery_threshold_percent"),
      integer_field(config, "minimum_safety_distance_cells"),
      integer_field(config, "default_workstation_capacity"),
  };
  return request;
}

json::Value result_to_value(const validator::ValidationResult& result) {
  json::Value::Array errors;
  errors.reserve(result.errors.size());
  for (const auto& error : result.errors) {
    errors.emplace_back(json::Value::Object{
        {"amr_id", json::Value(error.amr_id)},
        {"code", json::Value(error.code)},
        {"constraint", json::Value(error.constraint)},
        {"coordinate", nullable_position(error.coordinate)},
        {"limit", nullable_number(error.limit)},
        {"message", json::Value(error.message)},
        {"observed", nullable_number(error.observed)},
        {"order_id", json::Value(error.order_id)},
        {"path_index", json::Value(static_cast<double>(error.path_index))},
        {"related_amr_id", json::Value(error.related_amr_id)},
        {"related_coordinate", nullable_position(error.related_coordinate)},
        {"related_order_id", json::Value(error.related_order_id)},
        {"related_path_index", json::Value(static_cast<double>(error.related_path_index))},
        {"related_task_id", json::Value(error.related_task_id)},
        {"related_time", nullable_int(error.related_time)},
        {"task_id", json::Value(error.task_id)},
        {"time", nullable_int(error.time)},
    });
  }
  return json::Value::Object{
      {"errors", json::Value(std::move(errors))},
      {"error_count", json::Value(static_cast<double>(result.errors.size()))},
      {"ruleset_version", json::Value(result.ruleset_version)},
      {"schema_version", json::Value(result.schema_version)},
      {"status", json::Value(result.status)},
      {"stl", result.stl.has_value() ? stl_report_to_value(*result.stl) : json::Value(nullptr)},
      {"valid", json::Value(result.valid)},
  };
}

namespace {

json::Value scope_to_value(const stl::ScopeIdentity& scope) {
  return json::Value::Object{
      {"amr_id", json::Value(scope.amr_id)},
      {"kind", json::Value(stl::scope_kind_name(scope.kind))},
      {"order_id", json::Value(scope.order_id)},
      {"related_amr_id", json::Value(scope.related_amr_id)},
      {"related_order_id", json::Value(scope.related_order_id)},
      {"station_id", json::Value(scope.station_id)},
  };
}

}  // namespace

json::Value stl_report_to_value(const stl::MonitorReport& report) {
  json::Value::Array results;
  results.reserve(report.results.size());
  for (const auto& item : report.results) {
    results.emplace_back(json::Value::Object{
        {"coordinate", nullable_position(item.coordinate)},
        {"formula_id", json::Value(item.formula_id)},
        {"narrow_pass", json::Value(item.narrow_pass)},
        {"related_coordinate", nullable_position(item.related_coordinate)},
        {"robustness", nullable_number(item.robustness)},
        {"satisfied", json::Value(item.satisfied)},
        {"scope", scope_to_value(item.scope)},
        {"vacuous", json::Value(item.vacuous)},
        {"weakest_time", nullable_int(item.weakest_time)},
    });
  }
  return json::Value::Object{
      {"enforcement", json::Value(stl::enforcement_name(report.enforcement))},
      {"formula_count", json::Value(static_cast<double>(report.formula_count))},
      {"instance_count", json::Value(static_cast<double>(report.instance_count))},
      {"min_robustness", nullable_number(report.min_robustness)},
      {"min_robustness_formula_id", nullable_string(report.min_robustness_formula_id)},
      {"min_robustness_scope", report.min_robustness_scope.has_value()
                                   ? scope_to_value(*report.min_robustness_scope)
                                   : json::Value(nullptr)},
      {"narrow_pass_count", json::Value(static_cast<double>(report.narrow_pass_count))},
      {"results", json::Value(std::move(results))},
      {"satisfied", json::Value(report.satisfied)},
      {"skip_reason", nullable_string(report.skip_reason)},
      {"spec_id", json::Value(report.spec_id)},
      {"spec_version", json::Value(report.spec_version)},
      {"status", json::Value(report.status)},
      {"violated_count", json::Value(static_cast<double>(report.violated_count))},
  };
}

json::Value error_dictionary_to_value() {
  json::Value::Array values;
  for (const auto& definition : validator::error_dictionary()) {
    values.emplace_back(json::Value::Object{
        {"code", json::Value(definition.code)},
        {"constraint", json::Value(definition.constraint)},
        {"description", json::Value(definition.description)},
        {"evidence_contract", json::Value(definition.evidence_contract)},
    });
  }
  return json::Value::Object{
      {"error_dictionary", json::Value(std::move(values))},
      {"ruleset_version", json::Value("p0-10.v1")},
      {"schema_version", json::Value("1.0")},
      {"status", json::Value("ok")},
  };
}

json::Value error_to_value(const std::string& code, const std::string& message) {
  return json::Value::Object{
      {"error", json::Value::Object{{"code", json::Value(code)},
                                      {"message", json::Value(message)}}},
      {"schema_version", json::Value("1.0")},
      {"status", json::Value("error")},
  };
}

}  // namespace amr::planner::validator_json
