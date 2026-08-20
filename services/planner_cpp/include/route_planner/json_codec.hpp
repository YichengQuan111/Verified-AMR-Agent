#pragma once

#include "route_planner/route_planner.hpp"
#include "task_allocator/json_codec.hpp"

#include <string>

namespace amr::planner::route_json {

// route_planner 的 JSON 边界复用 P0-08 的严格 JSON 解析器，但单独定义请求
// 字段和响应字段；这样新增路线契约不会改变任务分配器的 schema 或行为。
RouteRequest request_from_value(const json::Value& value);
json::Value result_to_value(const RoutePlanResult& result);
json::Value error_to_value(const std::string& code, const std::string& message);

}  // namespace amr::planner::route_json
