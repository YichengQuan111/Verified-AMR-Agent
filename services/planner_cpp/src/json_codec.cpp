#include "task_allocator/json_codec.hpp"

#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <initializer_list>
#include <limits>
#include <locale>
#include <sstream>
#include <utility>

namespace amr::planner::json {
namespace {

class Parser {
 public:
  explicit Parser(std::string_view text) : text_(text) {}

  Value parse_document() {
    skip_whitespace();
    Value value = parse_value();
    skip_whitespace();
    if (position_ != text_.size()) {
      fail("unexpected characters after the JSON document");
    }
    return value;
  }

 private:
  [[noreturn]] void fail(const std::string& message) const {
    throw ParseError(message + " at byte " + std::to_string(position_));
  }

  void skip_whitespace() {
    while (position_ < text_.size()) {
      const unsigned char character = static_cast<unsigned char>(text_[position_]);
      if (character != ' ' && character != '\t' && character != '\r' && character != '\n') {
        break;
      }
      ++position_;
    }
  }

  char peek() const {
    if (position_ >= text_.size()) {
      return '\0';
    }
    return text_[position_];
  }

  char take() {
    if (position_ >= text_.size()) {
      fail("unexpected end of JSON");
    }
    return text_[position_++];
  }

  void expect(char expected) {
    if (take() != expected) {
      fail(std::string("expected '") + expected + "'");
    }
  }

  Value parse_value() {
    skip_whitespace();
    switch (peek()) {
      case '{':
        return parse_object();
      case '[':
        return parse_array();
      case '"':
        return Value(parse_string());
      case 't':
        parse_literal("true");
        return Value(true);
      case 'f':
        parse_literal("false");
        return Value(false);
      case 'n':
        parse_literal("null");
        return Value(nullptr);
      default:
        if (peek() == '-' || (peek() >= '0' && peek() <= '9')) {
          return Value(parse_number());
        }
        fail("expected a JSON value");
    }
  }

  void parse_literal(std::string_view literal) {
    for (const char expected : literal) {
      if (take() != expected) {
        fail("invalid JSON literal");
      }
    }
  }

  Value parse_object() {
    expect('{');
    Value::Object object;
    skip_whitespace();
    if (peek() == '}') {
      take();
      return Value(std::move(object));
    }
    while (true) {
      skip_whitespace();
      if (peek() != '"') {
        fail("object keys must be strings");
      }
      const std::string key = parse_string();
      skip_whitespace();
      expect(':');
      Value child = parse_value();
      if (!object.emplace(key, std::move(child)).second) {
        fail("duplicate object key: " + key);
      }
      skip_whitespace();
      const char delimiter = take();
      if (delimiter == '}') {
        break;
      }
      if (delimiter != ',') {
        fail("expected ',' or '}' in object");
      }
    }
    return Value(std::move(object));
  }

  Value parse_array() {
    expect('[');
    Value::Array array;
    skip_whitespace();
    if (peek() == ']') {
      take();
      return Value(std::move(array));
    }
    while (true) {
      array.push_back(parse_value());
      skip_whitespace();
      const char delimiter = take();
      if (delimiter == ']') {
        break;
      }
      if (delimiter != ',') {
        fail("expected ',' or ']' in array");
      }
    }
    return Value(std::move(array));
  }

  std::uint32_t parse_hex_quad() {
    std::uint32_t value = 0;
    for (int count = 0; count < 4; ++count) {
      const char character = take();
      value <<= 4;
      if (character >= '0' && character <= '9') {
        value += static_cast<std::uint32_t>(character - '0');
      } else if (character >= 'a' && character <= 'f') {
        value += static_cast<std::uint32_t>(character - 'a' + 10);
      } else if (character >= 'A' && character <= 'F') {
        value += static_cast<std::uint32_t>(character - 'A' + 10);
      } else {
        fail("invalid unicode escape");
      }
    }
    return value;
  }

  static void append_utf8(std::string& output, std::uint32_t codepoint) {
    if (codepoint <= 0x7f) {
      output.push_back(static_cast<char>(codepoint));
    } else if (codepoint <= 0x7ff) {
      output.push_back(static_cast<char>(0xc0 | (codepoint >> 6)));
      output.push_back(static_cast<char>(0x80 | (codepoint & 0x3f)));
    } else if (codepoint <= 0xffff) {
      output.push_back(static_cast<char>(0xe0 | (codepoint >> 12)));
      output.push_back(static_cast<char>(0x80 | ((codepoint >> 6) & 0x3f)));
      output.push_back(static_cast<char>(0x80 | (codepoint & 0x3f)));
    } else {
      output.push_back(static_cast<char>(0xf0 | (codepoint >> 18)));
      output.push_back(static_cast<char>(0x80 | ((codepoint >> 12) & 0x3f)));
      output.push_back(static_cast<char>(0x80 | ((codepoint >> 6) & 0x3f)));
      output.push_back(static_cast<char>(0x80 | (codepoint & 0x3f)));
    }
  }

  std::string parse_string() {
    expect('"');
    std::string output;
    while (position_ < text_.size()) {
      const unsigned char character = static_cast<unsigned char>(take());
      if (character == '"') {
        return output;
      }
      if (character < 0x20) {
        fail("control characters must be escaped in JSON strings");
      }
      if (character != '\\') {
        output.push_back(static_cast<char>(character));
        continue;
      }
      const char escaped = take();
      switch (escaped) {
        case '"':
        case '\\':
        case '/':
          output.push_back(escaped);
          break;
        case 'b':
          output.push_back('\b');
          break;
        case 'f':
          output.push_back('\f');
          break;
        case 'n':
          output.push_back('\n');
          break;
        case 'r':
          output.push_back('\r');
          break;
        case 't':
          output.push_back('\t');
          break;
        case 'u': {
          std::uint32_t codepoint = parse_hex_quad();
          if (codepoint >= 0xd800 && codepoint <= 0xdbff) {
            if (take() != '\\' || take() != 'u') {
              fail("high unicode surrogate must be followed by a low surrogate");
            }
            const std::uint32_t low = parse_hex_quad();
            if (low < 0xdc00 || low > 0xdfff) {
              fail("invalid low unicode surrogate");
            }
            codepoint = 0x10000 + ((codepoint - 0xd800) << 10) + (low - 0xdc00);
          } else if (codepoint >= 0xdc00 && codepoint <= 0xdfff) {
            fail("unpaired low unicode surrogate");
          }
          append_utf8(output, codepoint);
          break;
        }
        default:
          fail("invalid escape sequence in JSON string");
      }
    }
    fail("unterminated JSON string");
  }

  double parse_number() {
    const std::size_t start = position_;
    if (peek() == '-') {
      ++position_;
    }
    if (peek() == '0') {
      ++position_;
      if (peek() >= '0' && peek() <= '9') {
        fail("leading zero is not valid JSON");
      }
    } else {
      if (peek() < '1' || peek() > '9') {
        fail("invalid JSON number");
      }
      while (peek() >= '0' && peek() <= '9') {
        ++position_;
      }
    }
    if (peek() == '.') {
      ++position_;
      if (peek() < '0' || peek() > '9') {
        fail("JSON number fraction requires digits");
      }
      while (peek() >= '0' && peek() <= '9') {
        ++position_;
      }
    }
    if (peek() == 'e' || peek() == 'E') {
      ++position_;
      if (peek() == '+' || peek() == '-') {
        ++position_;
      }
      if (peek() < '0' || peek() > '9') {
        fail("JSON number exponent requires digits");
      }
      while (peek() >= '0' && peek() <= '9') {
        ++position_;
      }
    }
    const std::string token(text_.substr(start, position_ - start));
    char* end = nullptr;
    errno = 0;
    const double value = std::strtod(token.c_str(), &end);
    if (errno == ERANGE || end != token.c_str() + token.size() || !std::isfinite(value)) {
      fail("JSON number is outside the supported finite range");
    }
    return value;
  }

  std::string_view text_;
  std::size_t position_{0};
};

const Value& required_field(const Value::Object& object, const char* key) {
  const auto iterator = object.find(key);
  if (iterator == object.end()) {
    throw ParseError(std::string("missing required field: ") + key);
  }
  return iterator->second;
}

void require_exact_keys(const Value::Object& object,
                        std::initializer_list<const char*> allowed_keys,
                        const char* object_name) {
  for (const auto& entry : object) {
    bool allowed = false;
    for (const char* key : allowed_keys) {
      if (entry.first == key) {
        allowed = true;
        break;
      }
    }
    if (!allowed) {
      throw ParseError(std::string("unknown field '") + entry.first + "' in " + object_name);
    }
  }
}

const Value::Object& object_field(const Value::Object& object, const char* key) {
  const Value& value = required_field(object, key);
  if (!value.is_object()) {
    throw ParseError(std::string("field '") + key + "' must be an object");
  }
  return value.as_object();
}

const Value::Array& array_field(const Value::Object& object, const char* key) {
  const Value& value = required_field(object, key);
  if (!value.is_array()) {
    throw ParseError(std::string("field '") + key + "' must be an array");
  }
  return value.as_array();
}

std::string string_field(const Value::Object& object, const char* key) {
  const Value& value = required_field(object, key);
  if (!value.is_string() || value.as_string().empty()) {
    throw ParseError(std::string("field '") + key + "' must be a non-empty string");
  }
  return value.as_string();
}

double number_field(const Value::Object& object, const char* key) {
  const Value& value = required_field(object, key);
  if (!value.is_number() || !std::isfinite(value.as_number())) {
    throw ParseError(std::string("field '") + key + "' must be a finite number");
  }
  return value.as_number();
}

int integer_field(const Value::Object& object, const char* key) {
  const double value = number_field(object, key);
  if (std::floor(value) != value || value < std::numeric_limits<int>::min() ||
      value > std::numeric_limits<int>::max()) {
    throw ParseError(std::string("field '") + key + "' must be an integer");
  }
  return static_cast<int>(value);
}

GridPosition position_value(const Value& value, const char* field_name) {
  if (!value.is_object()) {
    throw ParseError(std::string(field_name) + " must be an object");
  }
  const auto& object = value.as_object();
  require_exact_keys(object, {"x", "y"}, field_name);
  return GridPosition{integer_field(object, "x"), integer_field(object, "y")};
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
  throw ParseError("unknown task_status: " + value);
}

HealthStatus health_status_value(const std::string& value) {
  if (value == "HEALTHY") return HealthStatus::kHealthy;
  if (value == "DEGRADED") return HealthStatus::kDegraded;
  if (value == "FAULT") return HealthStatus::kFault;
  throw ParseError("unknown health_status: " + value);
}

ConnectionStatus connection_status_value(const std::string& value) {
  if (value == "ONLINE") return ConnectionStatus::kOnline;
  if (value == "DEGRADED") return ConnectionStatus::kDegraded;
  if (value == "OFFLINE") return ConnectionStatus::kOffline;
  throw ParseError("unknown connection_status: " + value);
}

std::vector<std::string> string_array(const Value::Object& object, const char* key) {
  const auto& array = array_field(object, key);
  std::vector<std::string> output;
  output.reserve(array.size());
  for (const auto& value : array) {
    if (!value.is_string() || value.as_string().empty()) {
      throw ParseError(std::string("array field '") + key + "' must contain non-empty strings");
    }
    output.push_back(value.as_string());
  }
  return output;
}

Value::Array strings_to_value(const std::vector<std::string>& values) {
  Value::Array output;
  output.reserve(values.size());
  for (const auto& value : values) {
    output.emplace_back(value);
  }
  return output;
}

std::string task_status_string(AMRTaskStatus value) {
  switch (value) {
    case AMRTaskStatus::kIdle: return "IDLE";
    case AMRTaskStatus::kToPickup: return "TO_PICKUP";
    case AMRTaskStatus::kLoading: return "LOADING";
    case AMRTaskStatus::kToDropoff: return "TO_DROPOFF";
    case AMRTaskStatus::kUnloading: return "UNLOADING";
    case AMRTaskStatus::kToCharge: return "TO_CHARGE";
    case AMRTaskStatus::kCharging: return "CHARGING";
    case AMRTaskStatus::kOffline: return "OFFLINE";
  }
  return "OFFLINE";
}

std::string health_status_string(HealthStatus value) {
  switch (value) {
    case HealthStatus::kHealthy: return "HEALTHY";
    case HealthStatus::kDegraded: return "DEGRADED";
    case HealthStatus::kFault: return "FAULT";
  }
  return "FAULT";
}

std::string connection_status_string(ConnectionStatus value) {
  switch (value) {
    case ConnectionStatus::kOnline: return "ONLINE";
    case ConnectionStatus::kDegraded: return "DEGRADED";
    case ConnectionStatus::kOffline: return "OFFLINE";
  }
  return "OFFLINE";
}

Value cost_components_to_value(const CostBreakdown& components) {
  return Value::Object{
      {"battery_risk", Value(components.battery_risk)},
      {"distance_to_pickup", Value(components.distance_to_pickup)},
      {"estimated_battery_after", Value(components.estimated_battery_after)},
      {"estimated_completion_time", Value(components.estimated_completion_time)},
      {"lateness_risk", Value(components.lateness_risk)},
      {"load_penalty", Value(components.load_penalty)},
      {"priority_bonus", Value(components.priority_bonus)},
      {"route_distance", Value(components.route_distance)},
      {"total_cost", Value(components.total_cost)},
  };
}

Value pair_to_value(const std::string& amr_id, const std::string& order_id,
                    const PairEvaluation& pair) {
  Value::Object object{
      {"amr_id", Value(amr_id)},
      {"cost", pair.feasible ? Value(pair.cost) : Value("INF")},
      {"order_id", Value(order_id)},
      {"reason_codes", Value(strings_to_value(pair.reason_codes))},
      {"reasons", Value(strings_to_value(pair.reasons))},
      {"status", Value(pair.feasible ? "feasible" : "infeasible")},
  };
  if (pair.components.has_value()) {
    object.emplace("components", cost_components_to_value(*pair.components));
  } else {
    object.emplace("components", Value(nullptr));
  }
  return Value(std::move(object));
}

}  // namespace

bool Value::is_null() const noexcept { return std::holds_alternative<std::nullptr_t>(data); }
bool Value::is_bool() const noexcept { return std::holds_alternative<bool>(data); }
bool Value::is_number() const noexcept { return std::holds_alternative<double>(data); }
bool Value::is_string() const noexcept { return std::holds_alternative<std::string>(data); }
bool Value::is_array() const noexcept { return std::holds_alternative<Array>(data); }
bool Value::is_object() const noexcept { return std::holds_alternative<Object>(data); }

const Value::Object& Value::as_object() const { return std::get<Object>(data); }
const Value::Array& Value::as_array() const { return std::get<Array>(data); }
const std::string& Value::as_string() const { return std::get<std::string>(data); }
double Value::as_number() const { return std::get<double>(data); }
bool Value::as_bool() const { return std::get<bool>(data); }

Value parse(std::string_view text) { return Parser(text).parse_document(); }

void serialize_string(std::ostringstream& output, const std::string& value) {
  output << '"';
  for (const unsigned char character : value) {
    switch (character) {
      case '"': output << "\\\""; break;
      case '\\': output << "\\\\"; break;
      case '\b': output << "\\b"; break;
      case '\f': output << "\\f"; break;
      case '\n': output << "\\n"; break;
      case '\r': output << "\\r"; break;
      case '\t': output << "\\t"; break;
      default:
        if (character < 0x20) {
          output << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                 << static_cast<int>(character) << std::dec << std::setfill(' ');
        } else {
          output << static_cast<char>(character);
        }
        break;
    }
  }
  output << '"';
}

void serialize_value(std::ostringstream& output, const Value& value) {
  if (value.is_null()) {
    output << "null";
  } else if (value.is_bool()) {
    output << (value.as_bool() ? "true" : "false");
  } else if (value.is_number()) {
    if (!std::isfinite(value.as_number())) {
      throw ParseError("cannot serialize a non-finite number");
    }
    if (value.as_number() == 0.0) {
      output << '0';
    } else {
      std::ostringstream number;
      number.imbue(std::locale::classic());
      number << std::setprecision(15) << value.as_number();
      output << number.str();
    }
  } else if (value.is_string()) {
    serialize_string(output, value.as_string());
  } else if (value.is_array()) {
    output << '[';
    const auto& array = value.as_array();
    for (std::size_t index = 0; index < array.size(); ++index) {
      if (index != 0) output << ',';
      serialize_value(output, array[index]);
    }
    output << ']';
  } else {
    output << '{';
    const auto& object = value.as_object();
    std::size_t index = 0;
    for (const auto& entry : object) {
      if (index++ != 0) output << ',';
      serialize_string(output, entry.first);
      output << ':';
      serialize_value(output, entry.second);
    }
    output << '}';
  }
}

std::string serialize(const Value& value) {
  std::ostringstream output;
  output.imbue(std::locale::classic());
  serialize_value(output, value);
  return output.str();
}

AllocationRequest request_from_value(const Value& value) {
  if (!value.is_object()) {
    throw ParseError("allocator request must be a JSON object");
  }
  const auto& root = value.as_object();
  require_exact_keys(root,
                     {"schema_version", "amrs", "orders", "location_positions",
                      "completed_order_ids", "weights", "config"},
                     "allocator request");
  if (string_field(root, "schema_version") != "1.0") {
    throw ParseError("schema_version must be \"1.0\"");
  }

  AllocationRequest request;
  for (const auto& value_item : array_field(root, "amrs")) {
    if (!value_item.is_object()) throw ParseError("each AMR must be an object");
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
    if (!value_item.is_object()) throw ParseError("each order must be an object");
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
        string_array(object, "dependencies"),
    });
  }

  const auto& locations = object_field(root, "location_positions");
  for (const auto& entry : locations) {
    if (entry.first.empty()) throw ParseError("location ID must not be empty");
    request.locations.push_back(Location{entry.first, position_value(entry.second, "location position")});
  }
  request.completed_order_ids = string_array(root, "completed_order_ids");

  const auto& weights = object_field(root, "weights");
  require_exact_keys(weights,
                     {"distance", "lateness_risk", "battery_risk", "load_penalty",
                      "priority_bonus"},
                     "weights");
  request.weights = CostWeights{
      number_field(weights, "distance"),
      number_field(weights, "lateness_risk"),
      number_field(weights, "battery_risk"),
      number_field(weights, "load_penalty"),
      number_field(weights, "priority_bonus"),
  };

  const auto& config = object_field(root, "config");
  require_exact_keys(config,
                     {"current_time", "maximum_load_kg", "travel_speed_cells_per_second",
                      "energy_per_cell_percent", "battery_warning_threshold_percent",
                      "new_task_battery_threshold_percent", "critical_battery_threshold_percent",
                      "battery_safety_reserve_percent"},
                     "config");
  request.config = AllocationConfig{
      integer_field(config, "current_time"),
      number_field(config, "maximum_load_kg"),
      number_field(config, "travel_speed_cells_per_second"),
      number_field(config, "energy_per_cell_percent"),
      number_field(config, "battery_warning_threshold_percent"),
      number_field(config, "new_task_battery_threshold_percent"),
      number_field(config, "critical_battery_threshold_percent"),
      number_field(config, "battery_safety_reserve_percent"),
  };
  return request;
}

Value result_to_value(const AllocationResult& result) {
  Value::Array matrix;
  Value::Array pair_evaluations;
  matrix.reserve(result.pair_evaluations.size());
  for (std::size_t amr_index = 0; amr_index < result.pair_evaluations.size(); ++amr_index) {
    Value::Array row;
    row.reserve(result.pair_evaluations[amr_index].size());
    for (std::size_t order_index = 0;
         order_index < result.pair_evaluations[amr_index].size(); ++order_index) {
      const auto& pair = result.pair_evaluations[amr_index][order_index];
      row.emplace_back(pair.feasible ? Value(pair.cost) : Value("INF"));
      pair_evaluations.emplace_back(pair_to_value(result.amr_ids[amr_index],
                                                  result.order_ids[order_index], pair));
    }
    matrix.emplace_back(std::move(row));
  }

  Value::Array assignments;
  for (const auto& assignment : result.assignments) {
    assignments.emplace_back(Value::Object{
        {"amr_id", Value(assignment.amr_id)},
        {"components", cost_components_to_value(assignment.components)},
        {"order_id", Value(assignment.order_id)},
    });
  }
  Value::Array unassigned_orders;
  for (const auto& order : result.unassigned_orders) {
    Value::Array candidates;
    for (const auto& candidate : order.candidate_reasons) {
      candidates.emplace_back(Value::Object{
          {"amr_id", Value(candidate.amr_id)},
          {"reason_codes", Value(strings_to_value(candidate.reason_codes))},
          {"reasons", Value(strings_to_value(candidate.reasons))},
      });
    }
    unassigned_orders.emplace_back(Value::Object{
        {"candidate_reasons", Value(std::move(candidates))},
        {"order_id", Value(order.order_id)},
        {"reason_code", Value(order.reason_code)},
        {"reason_codes", Value(strings_to_value(order.reason_codes))},
    });
  }

  return Value::Object{
      {"algorithm", Value(result.algorithm)},
      {"amr_ids", Value(strings_to_value(result.amr_ids))},
      {"assignments", Value(std::move(assignments))},
      {"cost_matrix", Value(std::move(matrix))},
      {"order_ids", Value(strings_to_value(result.order_ids))},
      {"pair_evaluations", Value(std::move(pair_evaluations))},
      {"schema_version", Value("1.0")},
      {"status", Value(result.status)},
      {"total_cost", Value(result.total_cost)},
      {"unassigned_amrs", Value(strings_to_value(result.unassigned_amrs))},
      {"unassigned_orders", Value(std::move(unassigned_orders))},
  };
}

Value error_to_value(const std::string& code, const std::string& message) {
  return Value::Object{
      {"error", Value::Object{{"code", Value(code)}, {"message", Value(message)}}},
      {"schema_version", Value("1.0")},
      {"status", Value("error")},
  };
}

}  // namespace amr::planner::json
