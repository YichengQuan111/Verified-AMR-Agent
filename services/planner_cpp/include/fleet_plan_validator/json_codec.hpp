#pragma once

#include "fleet_plan_validator/fleet_plan_validator.hpp"
#include "task_allocator/json_codec.hpp"

namespace amr::planner::validator_json {

// Validator 使用 P0-08 的严格 JSON 值模型，但单独维护本步字段白名单；这样
// 解析器不会因为复用底层 Value 就意外接受分配器字段、任意文件路径或 Prompt
// 注入字段。重复键由底层解析器拒绝，未知键由本模块 envelope 门禁拒绝。
validator::FleetPlanRequest request_from_value(const json::Value& value);
json::Value result_to_value(const validator::ValidationResult& result);
json::Value error_dictionary_to_value();
json::Value error_to_value(const std::string& code, const std::string& message);

// P1-1：STL 报告序列化。结果对象内的 `stl` 键在未传规约时为 null；报告中的
// 每条实例固定携带 formula_id、scope、satisfied、robustness、weakest_time、
// coordinate、vacuous、narrow_pass，便于 Trace 直接消费而不必解析 message。
json::Value stl_report_to_value(const stl::MonitorReport& report);

}  // namespace amr::planner::validator_json

