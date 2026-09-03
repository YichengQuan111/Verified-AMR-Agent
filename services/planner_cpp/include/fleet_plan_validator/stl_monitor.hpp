#pragma once

#include "task_allocator/json_codec.hpp"

#include <cstddef>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

// P1-1 STL（Signal Temporal Logic）离散时间、有限轨迹鲁棒度监控器。
//
// 设计原因：P0-10 规则验证器是整个系统的安全论证落点，但它自己只有 CTest
// 正反例，没有独立 oracle。本模块把同一批安全约束改写成形式化 STL 规约，
// 从 JSON/DSL 文件加载、独立于规则层重新提取信号并求值，作为第二判定层：
// 布尔结论必须与规则层一致（不一致即其中一方的 Bug），定量鲁棒度则额外给出
// “裕量有多大、最薄弱时刻在哪”，供 Trace、险胜记录和后续可验证奖励使用。
//
// 数据流：规约文本 → parse_formula()/parse_specification() → Formula AST；
// FleetPlanRequest → 信号轨迹（位置/电量/载荷/距离/事件裕量） → evaluate()
// 逐时刻计算布尔值与鲁棒度 → MonitorReport。范围边界：只做派发前离线验证，
// 不做仿真流在线监控、不做 SMT、不做模型检验。
namespace amr::planner::stl {

// 规约语法或语义错误。它和 json::ParseError 一样属于契约边界错误：规约文件
// 写错时进程必须以契约错误退出，而不是静默把计划放行（fail-closed）。
class SpecificationError final : public std::runtime_error {
 public:
  explicit SpecificationError(const std::string& message) : std::runtime_error(message) {}
};

// 原子谓词只支持“信号 比较 阈值”这一种线性形式：鲁棒度就是带符号裕量。
// 严格/非严格区分保留下来，是为了与规则层对同一阈值的边界判定逐字对齐
// （例如电量“不高于阈值即拒绝”是严格大于才通过）。
enum class ComparisonOperator { kGreaterEqual, kLessEqual, kGreater, kLess };

enum class NodeKind {
  kTrue,
  kAtom,
  kNot,
  kAnd,
  kOr,
  kImplies,
  kGlobally,
  kEventually,
  kUntil,
};

// 区间端点可以是字面整数，也可以是“命名参数 ± 偏移”，例如 `deadline` 或
// `release_time - 1`。命名参数在实例化时由具体作用域提供，并已换算成相对轨迹
// 起点 start_time 的偏移，因此 STL 区间保持标准的相对语义。
struct BoundExpression {
  bool infinite{false};
  std::string parameter;
  long long offset{0};
};

struct Interval {
  // 省略区间等价于 [0, +inf)，即从当前时刻到轨迹末尾。
  bool explicit_interval{false};
  BoundExpression lower;
  BoundExpression upper;
};

// 阈值同样允许命名参数（如 `battery_safety_reserve_percent`），避免把项目
// 冻结的 20/10/15 电量阈值再硬编码进规约文本。
struct Threshold {
  std::string parameter;
  double literal{0.0};
};

struct Formula {
  NodeKind kind{NodeKind::kTrue};
  std::string signal;
  ComparisonOperator op{ComparisonOperator::kGreaterEqual};
  Threshold threshold;
  Interval interval;
  std::vector<Formula> children;
};

// 解析 DSL 文本。文法（优先级从低到高）：
//   implies := or ( "->" or )?
//   or      := and ( "or" and )*
//   and     := until ( "and" until )*
//   until   := unary ( "U" interval? unary )?
//   unary   := "not" unary | "G" interval? unary | "F" interval? unary
//            | "(" implies ")" | "true" | atom
//   atom    := identifier ( ">=" | "<=" | ">" | "<" ) ( number | identifier )
//   interval:= "[" bound "," bound "]"，bound := integer | identifier (("+"|"-") integer)? | "inf"
Formula parse_formula(std::string_view text);

// 把 AST 反序列化成规范文本，用于文档、错误信息和解析往返测试。
std::string format_formula(const Formula& formula);

// 一条公式在车队计划中的实例化作用域：按订单、按 AMR、按 AMR 对、按工位
// 或按订单依赖边分别提供不同的信号集合。
enum class ScopeKind { kOrder, kAmr, kPair, kStation, kDependency };

// gate：STL 违反会作为 `stl_specification_violated` 错误进入 errors，使计划
// invalid；shadow：只记录报告，不改变规则层结论。两者都会输出完整鲁棒度。
enum class Enforcement { kGate, kShadow };

struct FormulaSpec {
  std::string id;
  ScopeKind scope{ScopeKind::kOrder};
  std::string description;
  std::string formula_text;
  Formula formula;
  // 该公式对应的规则层错误码；一致性核对按此映射逐条比对，空列表表示
  // 规则层没有对应规则（例如低电量限时充电）。
  std::vector<std::string> rule_codes;
  // 鲁棒度低于该阈值即记为“险胜”；缺省表示不统计该公式的险胜。
  std::optional<double> warn_below;
};

struct Specification {
  std::string schema_version{"1.0"};
  std::string spec_id;
  std::string spec_version;
  Enforcement enforcement{Enforcement::kGate};
  std::vector<std::string> charging_location_ids;
  std::vector<FormulaSpec> formulas;
};

const char* scope_kind_name(ScopeKind kind) noexcept;
const char* enforcement_name(Enforcement enforcement) noexcept;

// 从严格 JSON 值模型加载规约；未知字段、未知作用域、未知信号/参数名、重复
// 公式 ID 都会抛出 SpecificationError，规约文件不能“大致正确”。
Specification parse_specification(const json::Value& value);
Specification parse_specification_text(std::string_view text);
json::Value specification_to_value(const Specification& specification);

// 通用求值输入：每个信号是长度为 length 的离散采样序列，下标 0 对应 start_time。
struct SignalTrace {
  int start_time{0};
  std::size_t length{0};
  std::map<std::string, std::vector<double>> signals;
  std::map<std::string, double> parameters;
};

// 顶层求值结果（在轨迹起点求值）。robustness 可能为 ±inf：例如 G 的区间落在
// 轨迹之外时为空真（+inf），F 的区间为空时为假（-inf）；vacuous 标记这种情况。
// weakest_time 是决定鲁棒度取值的时刻：G/and 取最小值处，F/or/U 取最大值处。
struct Evaluation {
  bool satisfied{false};
  double robustness{0.0};
  bool vacuous{false};
  std::optional<int> weakest_time;
};

Evaluation evaluate(const Formula& formula, const SignalTrace& trace);

struct ScopeIdentity {
  ScopeKind kind{ScopeKind::kOrder};
  std::string order_id;
  std::string related_order_id;
  std::string amr_id;
  std::string related_amr_id;
  std::string station_id;
};

struct InstanceResult {
  std::string formula_id;
  ScopeIdentity scope;
  bool satisfied{false};
  std::optional<double> robustness;
  std::optional<int> weakest_time;
  // 最薄弱时刻 AMR（及相关 AMR）所在栅格，便于 Trace 直接定位而不必回放路径。
  std::optional<GridPosition> coordinate;
  std::optional<GridPosition> related_coordinate;
  bool vacuous{false};
  bool narrow_pass{false};
};

struct MonitorReport {
  std::string spec_id;
  std::string spec_version;
  Enforcement enforcement{Enforcement::kGate};
  // satisfied / violated / skipped；skipped 表示请求连轨迹都无法构造
  // （地图或时间域非法），此时规则层必然已经报错。
  std::string status{"skipped"};
  bool satisfied{false};
  std::string skip_reason;
  std::size_t formula_count{0};
  std::size_t instance_count{0};
  std::size_t violated_count{0};
  std::size_t narrow_pass_count{0};
  std::optional<double> min_robustness;
  std::string min_robustness_formula_id;
  std::optional<ScopeIdentity> min_robustness_scope;
  std::vector<InstanceResult> results;
};

}  // namespace amr::planner::stl
