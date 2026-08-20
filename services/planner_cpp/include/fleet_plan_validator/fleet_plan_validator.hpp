#pragma once

#include "route_planner/route_planner.hpp"

#include <map>
#include <optional>
#include <string>
#include <vector>

namespace amr::planner::validator {

// Validator 的规则配置独立于 P0-08 的分配代价和 P0-09 的运动代价。
// 这样同一份路径快照在不同调用方之间只会由确定性安全阈值裁决，不会让
// LLM、Prompt 或上游的“已验证”标记改变结论；后续若引入物料重量字段，
// 只需扩展 FleetPlanRoute，不必修改 P0-04 的 TransportOrder 契约。
struct ValidatorConfig {
  double maximum_load_kg{100.0};
  double energy_per_cell_percent{1.0};
  double battery_safety_reserve_percent{15.0};
  double new_task_battery_threshold_percent{20.0};
  double critical_battery_threshold_percent{10.0};
  int minimum_safety_distance_cells{1};
  int default_workstation_capacity{1};
};

// 一条待验证的完整 pickup → dropoff 路线。payload_kg 是 P0-04 订单没有
// 携带的本次执行物料重量，必须由上游确定性数据源填写；status/reason_code
// 只保留 P0-09 审计信息，Validator 不会据此放行或拒绝路线。
struct FleetPlanRoute {
  std::string amr_id;
  std::string order_id;
  double payload_kg{};
  int pickup_time{-1};
  int dropoff_time{-1};
  std::vector<RouteStep> path;
  std::string status{"planned"};
  std::string planner_reason_code;
  std::string planner_reason;
};

// Validator 请求必须携带内存中的地图快照和路径，environment_ref 只用于
// 审计关联，绝不触发文件读取或按路径加载“可信地图”。缺失任何约束配置
// 都会在解析边界或验证结果中失败，不能由模型猜默认安全值。
struct FleetPlanRequest {
  std::string environment_ref;
  RouteMap map;
  std::vector<AMRState> amrs;
  std::vector<TransportOrder> orders;
  std::vector<Location> locations;
  std::vector<std::string> completed_order_ids;
  std::vector<FleetPlanRoute> routes;
  int start_time{0};
  int max_time{120};
  ValidatorConfig config;
  std::map<std::string, int> workstation_capacities;
  std::string ruleset_version{"p0-10.v1"};
};

struct ValidationEvidence {
  std::string code;
  std::string constraint;
  std::string message;

  // task_id/order_id 是同一稳定业务任务的两个审计别名，保留二者可让
  // P0-04 订单消费者和后续 PlanTask 追踪器都能直接定位，而无需猜字段。
  std::string task_id;
  std::string related_task_id;
  std::string order_id;
  std::string related_order_id;
  std::string amr_id;
  std::string related_amr_id;
  std::optional<GridPosition> coordinate;
  std::optional<GridPosition> related_coordinate;
  std::optional<int> time;
  std::optional<int> related_time;
  std::optional<double> observed;
  std::optional<double> limit;
  int path_index{-1};
  int related_path_index{-1};
};

// 错误字典是机器可读结果和人类文档的共同来源。evidence_contract 固定列出
// 该类错误至少应带出的定位字段，避免新增规则时只返回一个模糊布尔值。
struct ValidationErrorDefinition {
  std::string code;
  std::string constraint;
  std::string description;
  std::string evidence_contract;
};

struct ValidationResult {
  std::string schema_version{"1.0"};
  std::string ruleset_version{"p0-10.v1"};
  std::string status{"invalid"};  // valid 或 invalid；两者都是已处理的业务结果
  bool valid{false};
  std::vector<ValidationEvidence> errors;
};

// 返回按稳定错误码排序的只读错误字典；调用方可用于 --error-dictionary，
// 也可在 Trace 中记录规则版本。该接口不暴露可变全局状态。
const std::vector<ValidationErrorDefinition>& error_dictionary() noexcept;

// 对结构化计划执行完整确定性验证。非法计划通过 errors 返回，函数不会把
// “发现违规”当作 C++ 异常；只有 JSON/契约边界错误才由 codec 抛出 ParseError。
ValidationResult validate_fleet_plan(const FleetPlanRequest& request);

}  // namespace amr::planner::validator

