#pragma once

#include "task_allocator/task_allocator.hpp"

#include <cstddef>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace amr::planner {

// 路网边使用有向端点表示；one_way_edges 只限制同一无向相邻单元之间的
// 允许方向，blocked_edges 则表示当前环境版本中不可通过的有向边。
struct RouteEdge {
  GridPosition from;
  GridPosition to;
};

// 地图尺寸允许测试使用小地图，但生产默认仍是 P0 冻结的 30×20 栅格。
// 所有阻塞/单向信息都随本次请求传入，规划器不会从路径或文件系统猜测地图状态。
struct RouteMap {
  int width{30};
  int height{20};
  std::vector<GridPosition> blocked_cells;
  std::vector<RouteEdge> blocked_edges;
  std::vector<RouteEdge> one_way_edges;
};

// 一个分配结果中的 AMR—订单绑定。订单的 pickup/dropoff/优先级等信息从
// RouteRequest.orders 中按 order_id 查找，避免复制或修改 P0-04 TransportOrder。
struct RouteAssignment {
  std::string amr_id;
  std::string order_id;
};

// 运动代价显式配置，避免把等待或转向偷偷当作零成本动作。代价只影响
// 路径选择，不改变每个动作占用一个离散时间步的安全语义。
struct RouteCostConfig {
  double move_cost{1.0};
  double turn_cost{0.25};
  double wait_cost{1.0};
};

enum class RouteAlgorithm { kAStar, kDijkstra };

enum class RouteAction {
  kStart,
  kMove,
  kTurnLeft,
  kTurnRight,
  kWait,
};

// path 中每个元素表示 AMR 在该离散时刻的状态；action 是从前一个元素
// 到当前元素所执行的动作，首元素固定为 start。这样可以直接验证边界、障碍、
// 顶点冲突和交换边冲突，而不需要从结果反推隐藏的时间轴。
struct RouteStep {
  GridPosition position;
  int heading{};
  int time{};
  RouteAction action{RouteAction::kStart};
  double g_cost{};
};

struct PlannedRoute {
  std::string amr_id;
  std::string order_id;
  int priority{};
  std::string status;  // planned 或 infeasible
  std::string reason_code;
  std::string reason;
  int pickup_time{-1};
  int dropoff_time{-1};
  double total_cost{};
  std::size_t expanded_states{};
  std::vector<RouteStep> path;
};

struct RouteRequest {
  std::string environment_ref;
  RouteMap map;
  std::vector<AMRState> amrs;
  std::vector<TransportOrder> orders;
  std::vector<Location> locations;
  std::vector<RouteAssignment> assignments;
  std::vector<std::string> completed_order_ids;
  int start_time{0};
  int max_time{120};
  RouteCostConfig costs;
};

struct RoutePlanResult {
  std::string algorithm;
  std::string status;  // complete 或 infeasible；后者是业务不可行，不是进程错误
  std::size_t planned_count{};
  std::size_t total_expanded_states{};
  double total_cost{};
  std::size_t cell_reservation_count{};
  std::size_t edge_reservation_count{};
  std::vector<PlannedRoute> routes;
};

class RouteError final : public std::runtime_error {
 public:
  RouteError(std::string code, std::string message);

  const std::string& code() const noexcept { return code_; }

 private:
  std::string code_;
};

// 时空预约表是规划器和 Validator 共享的硬约束组件：cell,t 阻止同一时刻
// 占用同一单元，edge,t 同时检查正向和反向边，专门阻止交换边冲突。完成位置
// 会保持预约到 max_time，防止后续车辆把已停靠 AMR 当成可穿越的空位。
class ReservationTable final {
 public:
  explicit ReservationTable(int max_time);

  int max_time() const noexcept { return max_time_; }
  bool is_cell_reserved(const GridPosition& cell, int time) const noexcept;
  bool is_edge_reserved(const GridPosition& from,
                        const GridPosition& to,
                        int departure_time) const noexcept;
  bool can_transition(const GridPosition& from,
                     const GridPosition& to,
                     int departure_time) const noexcept;

  void reserve_path(const std::vector<RouteStep>& path, int hold_until);

  std::size_t cell_reservation_count() const noexcept { return cells_.size(); }
  std::size_t edge_reservation_count() const noexcept { return edges_.size(); }

 private:
  struct CellKey {
    int x{};
    int y{};
    int time{};
    bool operator<(const CellKey& other) const noexcept;
  };

  struct EdgeKey {
    int from_x{};
    int from_y{};
    int to_x{};
    int to_y{};
    int time{};
    bool operator<(const EdgeKey& other) const noexcept;
  };

  int max_time_;
  std::set<CellKey> cells_;
  std::set<EdgeKey> edges_;
};

// 生产路径规划：按订单优先级、release_time、稳定 ID 排序，逐车调用 A* 并
// 把已生成路径写入预约表。任何一台车无安全路径都会使结果明确为 infeasible。
RoutePlanResult plan_routes_astar(const RouteRequest& request);

// 正确性基线：独立的时间扩展 Dijkstra，不调用 A*、不读取 A* 的开放/关闭集，
// 用于比较最优代价和验证障碍/预约约束实现。
RoutePlanResult plan_routes_dijkstra(const RouteRequest& request);

// 统一的显式算法入口；调用方若要比较算法应分别调用两个独立入口，而不是把
// Dijkstra 当作生产失败后的隐式 fallback。
RoutePlanResult plan_multi_amr_routes(const RouteRequest& request,
                                       RouteAlgorithm algorithm);

const char* route_action_name(RouteAction action) noexcept;

}  // namespace amr::planner
