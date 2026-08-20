#pragma once

#include <cstddef>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace amr::planner {

// 内部 INF 只用于 Hungarian 的数值矩阵；跨语言 JSON 使用字符串 "INF"，避免
// 非标准 JSON 的 Infinity 污染 Python/JavaScript 等消费者。
inline constexpr double kInternalInf = 1.0e12;

struct GridPosition {
  int x{};
  int y{};
};

enum class AMRTaskStatus {
  kIdle,
  kToPickup,
  kLoading,
  kToDropoff,
  kUnloading,
  kToCharge,
  kCharging,
  kOffline,
};

enum class HealthStatus { kHealthy, kDegraded, kFault };

enum class ConnectionStatus { kOnline, kDegraded, kOffline };

struct AMRState {
  std::string amr_id;
  GridPosition position;
  int heading{};
  double battery{};
  double load{};
  AMRTaskStatus task_status{AMRTaskStatus::kIdle};
  HealthStatus health_status{HealthStatus::kHealthy};
  ConnectionStatus connection_status{ConnectionStatus::kOnline};
};

struct TransportOrder {
  std::string order_id;
  std::string material_id;
  std::string pickup;
  std::string dropoff;
  int priority{};
  int release_time{};
  int deadline{};
  std::vector<std::string> dependencies;
};

// P0-04 的订单只携带工位 ID，位置快照通过本模块请求 envelope 单独传入。
// 这样不会修改既有 TransportOrder，也不会让分配器从不受控的文件路径读取地图。
struct Location {
  std::string location_id;
  GridPosition position;
};

struct CostWeights {
  double distance{1.0};
  double lateness_risk{10.0};
  double battery_risk{5.0};
  double load_penalty{2.0};
  double priority_bonus{1.0};
};

struct AllocationConfig {
  int current_time{};
  double maximum_load_kg{100.0};
  double travel_speed_cells_per_second{1.0};
  double energy_per_cell_percent{1.0};
  double battery_warning_threshold_percent{30.0};
  double new_task_battery_threshold_percent{20.0};
  double critical_battery_threshold_percent{10.0};
  double battery_safety_reserve_percent{15.0};
};

struct AllocationRequest {
  std::vector<AMRState> amrs;
  std::vector<TransportOrder> orders;
  std::vector<Location> locations;
  std::vector<std::string> completed_order_ids;
  CostWeights weights;
  AllocationConfig config;
};

struct CostBreakdown {
  double distance_to_pickup{};
  double route_distance{};
  double estimated_completion_time{};
  double lateness_risk{};
  double priority_bonus{};
  double battery_risk{};
  double estimated_battery_after{};
  double load_penalty{};
  double total_cost{};
};

struct PairEvaluation {
  bool feasible{false};
  double cost{kInternalInf};
  std::vector<std::string> reason_codes;
  std::vector<std::string> reasons;
  std::optional<CostBreakdown> components;
};

struct Assignment {
  std::string amr_id;
  std::string order_id;
  CostBreakdown components;
};

struct CandidateReason {
  std::string amr_id;
  std::vector<std::string> reason_codes;
  std::vector<std::string> reasons;
};

struct UnassignedOrder {
  std::string order_id;
  std::string reason_code;
  std::vector<std::string> reason_codes;
  std::vector<CandidateReason> candidate_reasons;
};

struct AllocationResult {
  std::string algorithm;
  std::string status;
  std::vector<std::string> amr_ids;
  std::vector<std::string> order_ids;
  std::vector<std::vector<PairEvaluation>> pair_evaluations;
  std::vector<Assignment> assignments;
  std::vector<UnassignedOrder> unassigned_orders;
  std::vector<std::string> unassigned_amrs;
  double total_cost{};
};

class AllocationError final : public std::runtime_error {
 public:
  AllocationError(std::string code, std::string message);

  const std::string& code() const noexcept { return code_; }

 private:
  std::string code_;
};

// 生产分配器：先做确定性可行性筛选，再在含 dummy 行/列的矩形代价矩阵上运行
// Hungarian。dummy 只用于表达“订单/车辆未匹配”，不会把 INF 组合伪装成合法任务。
AllocationResult allocate_hungarian(const AllocationRequest& request);

// 正确性/策略基线：每个订单独立选择尚未占用且距离 pickup 最近的空闲 AMR。
// 该函数不调用 Hungarian，也不读取生产分配器的内部匹配结果。
AllocationResult allocate_nearest_idle(const AllocationRequest& request);

}  // namespace amr::planner
