#pragma once

#include "task_allocator/task_allocator.hpp"

#include <map>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <variant>
#include <vector>

namespace amr::planner::json {

class ParseError final : public std::runtime_error {
 public:
  explicit ParseError(const std::string& message) : std::runtime_error(message) {}
};

// 这是分配器边界所需的最小 JSON 值模型，不引入第三方运行库。对象使用 map
// 保证序列化键顺序稳定；解析器严格拒绝重复键和未知业务字段，避免跨语言调用时
// “后一个字段覆盖前一个字段”或拼写错误静默改变调度结果。
struct Value {
  using Array = std::vector<Value>;
  using Object = std::map<std::string, Value>;
  using Storage = std::variant<std::nullptr_t, bool, double, std::string, Array, Object>;

  Storage data;

  Value() : data(nullptr) {}
  Value(std::nullptr_t) : data(nullptr) {}
  Value(bool value) : data(value) {}
  Value(double value) : data(value) {}
  Value(const char* value) : data(std::string(value)) {}
  Value(std::string value) : data(std::move(value)) {}
  Value(Array value) : data(std::move(value)) {}
  Value(Object value) : data(std::move(value)) {}

  bool is_null() const noexcept;
  bool is_bool() const noexcept;
  bool is_number() const noexcept;
  bool is_string() const noexcept;
  bool is_array() const noexcept;
  bool is_object() const noexcept;

  const Object& as_object() const;
  const Array& as_array() const;
  const std::string& as_string() const;
  double as_number() const;
  bool as_bool() const;
};

Value parse(std::string_view text);
std::string serialize(const Value& value);

AllocationRequest request_from_value(const Value& value);
Value result_to_value(const AllocationResult& result);
Value error_to_value(const std::string& code, const std::string& message);

}  // namespace amr::planner::json
