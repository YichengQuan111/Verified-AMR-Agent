#include "fleet_plan_validator/stl_monitor.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <initializer_list>
#include <iomanip>
#include <limits>
#include <set>
#include <sstream>
#include <utility>

namespace amr::planner::stl {
namespace {

// 与规则层 kEpsilon 相同，保证同一阈值在两层的边界判定一致。
constexpr double kEpsilon = 1.0e-9;
constexpr double kInfinity = std::numeric_limits<double>::infinity();
constexpr std::size_t kMaxFormulaLength = 4096U;
constexpr std::size_t kMaxFormulaDepth = 64U;

// ---------------------------------------------------------------------------
// 词法分析
// ---------------------------------------------------------------------------

enum class TokenKind {
  kIdentifier,
  kNumber,
  kGreaterEqual,
  kLessEqual,
  kGreater,
  kLess,
  kImplies,
  kLeftParen,
  kRightParen,
  kLeftBracket,
  kRightBracket,
  kComma,
  kPlus,
  kMinus,
  kEnd,
};

struct Token {
  TokenKind kind{TokenKind::kEnd};
  std::string text;
  double number{0.0};
  bool integral{false};
  std::size_t position{0};
};

std::vector<Token> tokenize(std::string_view text) {
  if (text.size() > kMaxFormulaLength) {
    throw SpecificationError("STL 公式文本超过 4096 字符上限");
  }
  std::vector<Token> tokens;
  std::size_t index = 0;
  while (index < text.size()) {
    const char current = text[index];
    if (std::isspace(static_cast<unsigned char>(current)) != 0) {
      ++index;
      continue;
    }
    Token token;
    token.position = index;
    if (std::isalpha(static_cast<unsigned char>(current)) != 0 || current == '_') {
      std::size_t end = index;
      while (end < text.size() &&
             (std::isalnum(static_cast<unsigned char>(text[end])) != 0 || text[end] == '_')) {
        ++end;
      }
      token.kind = TokenKind::kIdentifier;
      token.text = std::string(text.substr(index, end - index));
      index = end;
    } else if (std::isdigit(static_cast<unsigned char>(current)) != 0) {
      std::size_t end = index;
      bool integral = true;
      while (end < text.size() &&
             (std::isdigit(static_cast<unsigned char>(text[end])) != 0 || text[end] == '.')) {
        if (text[end] == '.') integral = false;
        ++end;
      }
      token.kind = TokenKind::kNumber;
      token.text = std::string(text.substr(index, end - index));
      token.integral = integral;
      try {
        std::size_t consumed = 0;
        token.number = std::stod(token.text, &consumed);
        if (consumed != token.text.size()) throw std::invalid_argument("trailing");
      } catch (const std::exception&) {
        throw SpecificationError("STL 公式数字非法: " + token.text);
      }
      if (!std::isfinite(token.number)) {
        throw SpecificationError("STL 公式数字必须有限: " + token.text);
      }
      index = end;
    } else {
      const auto starts_with = [&](std::string_view prefix) {
        return text.substr(index, prefix.size()) == prefix;
      };
      if (starts_with(">=")) {
        token.kind = TokenKind::kGreaterEqual;
        index += 2;
      } else if (starts_with("<=")) {
        token.kind = TokenKind::kLessEqual;
        index += 2;
      } else if (starts_with("->")) {
        token.kind = TokenKind::kImplies;
        index += 2;
      } else {
        switch (current) {
          case '>':
            token.kind = TokenKind::kGreater;
            break;
          case '<':
            token.kind = TokenKind::kLess;
            break;
          case '(':
            token.kind = TokenKind::kLeftParen;
            break;
          case ')':
            token.kind = TokenKind::kRightParen;
            break;
          case '[':
            token.kind = TokenKind::kLeftBracket;
            break;
          case ']':
            token.kind = TokenKind::kRightBracket;
            break;
          case ',':
            token.kind = TokenKind::kComma;
            break;
          case '+':
            token.kind = TokenKind::kPlus;
            break;
          case '-':
            token.kind = TokenKind::kMinus;
            break;
          default:
            throw SpecificationError(std::string("STL 公式含非法字符: '") + current +
                                     "' 位置 " + std::to_string(index));
        }
        ++index;
      }
    }
    tokens.push_back(std::move(token));
  }
  Token end;
  end.kind = TokenKind::kEnd;
  end.position = text.size();
  tokens.push_back(std::move(end));
  return tokens;
}

// ---------------------------------------------------------------------------
// 递归下降解析
// ---------------------------------------------------------------------------

bool is_keyword(const Token& token, std::string_view keyword) {
  return token.kind == TokenKind::kIdentifier && token.text == keyword;
}

bool is_reserved_word(const std::string& word) {
  static const std::set<std::string> reserved = {"and", "or", "not", "G", "F", "U", "true",
                                                 "inf"};
  return reserved.count(word) != 0U;
}

class Parser {
 public:
  explicit Parser(std::vector<Token> tokens) : tokens_(std::move(tokens)) {}

  Formula parse() {
    Formula formula = parse_implies(0);
    expect(TokenKind::kEnd, "公式末尾有多余内容");
    return formula;
  }

 private:
  const Token& peek() const { return tokens_[index_]; }
  const Token& advance() { return tokens_[index_++]; }

  void expect(TokenKind kind, const std::string& message) {
    if (peek().kind != kind) {
      throw SpecificationError("STL 公式解析失败: " + message + "（位置 " +
                               std::to_string(peek().position) + "）");
    }
    advance();
  }

  void check_depth(std::size_t depth) const {
    if (depth > kMaxFormulaDepth) throw SpecificationError("STL 公式嵌套深度超过 64");
  }

  Formula parse_implies(std::size_t depth) {
    check_depth(depth);
    Formula left = parse_or(depth + 1);
    if (peek().kind == TokenKind::kImplies) {
      advance();
      // 蕴含右结合：a -> b -> c 读作 a -> (b -> c)。
      Formula right = parse_implies(depth + 1);
      Formula node;
      node.kind = NodeKind::kImplies;
      node.children.push_back(std::move(left));
      node.children.push_back(std::move(right));
      return node;
    }
    return left;
  }

  Formula parse_or(std::size_t depth) {
    check_depth(depth);
    Formula left = parse_and(depth + 1);
    while (is_keyword(peek(), "or")) {
      advance();
      Formula right = parse_and(depth + 1);
      Formula node;
      node.kind = NodeKind::kOr;
      node.children.push_back(std::move(left));
      node.children.push_back(std::move(right));
      left = std::move(node);
    }
    return left;
  }

  Formula parse_and(std::size_t depth) {
    check_depth(depth);
    Formula left = parse_until(depth + 1);
    while (is_keyword(peek(), "and")) {
      advance();
      Formula right = parse_until(depth + 1);
      Formula node;
      node.kind = NodeKind::kAnd;
      node.children.push_back(std::move(left));
      node.children.push_back(std::move(right));
      left = std::move(node);
    }
    return left;
  }

  Formula parse_until(std::size_t depth) {
    check_depth(depth);
    Formula left = parse_unary(depth + 1);
    if (is_keyword(peek(), "U")) {
      advance();
      Formula node;
      node.kind = NodeKind::kUntil;
      node.interval = parse_optional_interval();
      Formula right = parse_unary(depth + 1);
      node.children.push_back(std::move(left));
      node.children.push_back(std::move(right));
      return node;
    }
    return left;
  }

  Formula parse_unary(std::size_t depth) {
    check_depth(depth);
    if (is_keyword(peek(), "not")) {
      advance();
      Formula node;
      node.kind = NodeKind::kNot;
      node.children.push_back(parse_unary(depth + 1));
      return node;
    }
    if (is_keyword(peek(), "G") || is_keyword(peek(), "F")) {
      const bool globally = peek().text == "G";
      advance();
      Formula node;
      node.kind = globally ? NodeKind::kGlobally : NodeKind::kEventually;
      node.interval = parse_optional_interval();
      node.children.push_back(parse_unary(depth + 1));
      return node;
    }
    if (peek().kind == TokenKind::kLeftParen) {
      advance();
      Formula inner = parse_implies(depth + 1);
      expect(TokenKind::kRightParen, "缺少右括号");
      return inner;
    }
    if (is_keyword(peek(), "true")) {
      advance();
      Formula node;
      node.kind = NodeKind::kTrue;
      return node;
    }
    return parse_atom();
  }

  Formula parse_atom() {
    if (peek().kind != TokenKind::kIdentifier || is_reserved_word(peek().text)) {
      throw SpecificationError("STL 公式解析失败: 期望信号名（位置 " +
                               std::to_string(peek().position) + "）");
    }
    Formula node;
    node.kind = NodeKind::kAtom;
    node.signal = advance().text;
    switch (peek().kind) {
      case TokenKind::kGreaterEqual:
        node.op = ComparisonOperator::kGreaterEqual;
        break;
      case TokenKind::kLessEqual:
        node.op = ComparisonOperator::kLessEqual;
        break;
      case TokenKind::kGreater:
        node.op = ComparisonOperator::kGreater;
        break;
      case TokenKind::kLess:
        node.op = ComparisonOperator::kLess;
        break;
      default:
        throw SpecificationError("STL 公式解析失败: 信号 " + node.signal +
                                 " 后必须跟 >=、<=、> 或 <");
    }
    advance();
    node.threshold = parse_threshold();
    return node;
  }

  Threshold parse_threshold() {
    Threshold threshold;
    bool negative = false;
    if (peek().kind == TokenKind::kMinus) {
      advance();
      negative = true;
    }
    if (peek().kind == TokenKind::kNumber) {
      threshold.literal = negative ? -advance().number : advance().number;
      return threshold;
    }
    if (negative) throw SpecificationError("STL 公式解析失败: 负号后必须是数字");
    if (peek().kind == TokenKind::kIdentifier && !is_reserved_word(peek().text)) {
      threshold.parameter = advance().text;
      return threshold;
    }
    throw SpecificationError("STL 公式解析失败: 阈值必须是数字或参数名（位置 " +
                             std::to_string(peek().position) + "）");
  }

  Interval parse_optional_interval() {
    Interval interval;
    if (peek().kind != TokenKind::kLeftBracket) return interval;
    advance();
    interval.explicit_interval = true;
    interval.lower = parse_bound(false);
    expect(TokenKind::kComma, "区间必须用逗号分隔上下界");
    interval.upper = parse_bound(true);
    expect(TokenKind::kRightBracket, "区间缺少右方括号");
    if (interval.lower.infinite) throw SpecificationError("STL 区间下界不能是 inf");
    if (interval.lower.parameter.empty() && interval.upper.parameter.empty() &&
        !interval.upper.infinite && interval.lower.offset > interval.upper.offset) {
      throw SpecificationError("STL 字面区间下界不能大于上界");
    }
    return interval;
  }

  BoundExpression parse_bound(bool allow_infinite) {
    BoundExpression bound;
    if (is_keyword(peek(), "inf")) {
      if (!allow_infinite) throw SpecificationError("STL 区间下界不能是 inf");
      advance();
      bound.infinite = true;
      return bound;
    }
    if (peek().kind == TokenKind::kNumber) {
      const Token& token = advance();
      if (!token.integral || token.number < 0.0) {
        throw SpecificationError("STL 字面区间端点必须是非负整数: " + token.text);
      }
      bound.offset = static_cast<long long>(token.number);
      return bound;
    }
    if (peek().kind == TokenKind::kIdentifier && !is_reserved_word(peek().text)) {
      bound.parameter = advance().text;
      if (peek().kind == TokenKind::kPlus || peek().kind == TokenKind::kMinus) {
        const bool negative = advance().kind == TokenKind::kMinus;
        if (peek().kind != TokenKind::kNumber || !peek().integral) {
          throw SpecificationError("STL 区间参数偏移必须是整数");
        }
        const long long magnitude = static_cast<long long>(advance().number);
        bound.offset = negative ? -magnitude : magnitude;
      }
      return bound;
    }
    throw SpecificationError("STL 区间端点必须是整数、参数名或 inf（位置 " +
                             std::to_string(peek().position) + "）");
  }

  std::vector<Token> tokens_;
  std::size_t index_{0};
};

// ---------------------------------------------------------------------------
// 规范文本输出
// ---------------------------------------------------------------------------

std::string number_text(double value) {
  if (std::trunc(value) == value && std::fabs(value) < 1.0e15) {
    return std::to_string(static_cast<long long>(value));
  }
  std::ostringstream stream;
  stream.imbue(std::locale::classic());
  stream << std::setprecision(15) << value;
  return stream.str();
}

std::string bound_text(const BoundExpression& bound) {
  if (bound.infinite) return "inf";
  if (bound.parameter.empty()) return std::to_string(bound.offset);
  if (bound.offset == 0) return bound.parameter;
  return bound.parameter + (bound.offset > 0 ? " + " : " - ") +
         std::to_string(bound.offset > 0 ? bound.offset : -bound.offset);
}

std::string interval_text(const Interval& interval) {
  if (!interval.explicit_interval) return "";
  return "[" + bound_text(interval.lower) + ", " + bound_text(interval.upper) + "]";
}

const char* operator_text(ComparisonOperator op) noexcept {
  switch (op) {
    case ComparisonOperator::kGreaterEqual:
      return ">=";
    case ComparisonOperator::kLessEqual:
      return "<=";
    case ComparisonOperator::kGreater:
      return ">";
    case ComparisonOperator::kLess:
      return "<";
  }
  return "?";
}

std::string format_node(const Formula& formula) {
  switch (formula.kind) {
    case NodeKind::kTrue:
      return "true";
    case NodeKind::kAtom:
      return formula.signal + " " + operator_text(formula.op) + " " +
             (formula.threshold.parameter.empty() ? number_text(formula.threshold.literal)
                                                  : formula.threshold.parameter);
    case NodeKind::kNot:
      return "not (" + format_node(formula.children.at(0)) + ")";
    case NodeKind::kAnd:
      return "(" + format_node(formula.children.at(0)) + ") and (" +
             format_node(formula.children.at(1)) + ")";
    case NodeKind::kOr:
      return "(" + format_node(formula.children.at(0)) + ") or (" +
             format_node(formula.children.at(1)) + ")";
    case NodeKind::kImplies:
      return "(" + format_node(formula.children.at(0)) + ") -> (" +
             format_node(formula.children.at(1)) + ")";
    case NodeKind::kGlobally:
      return "G" + interval_text(formula.interval) + "(" +
             format_node(formula.children.at(0)) + ")";
    case NodeKind::kEventually:
      return "F" + interval_text(formula.interval) + "(" +
             format_node(formula.children.at(0)) + ")";
    case NodeKind::kUntil:
      return "(" + format_node(formula.children.at(0)) + ") U" +
             interval_text(formula.interval) + " (" + format_node(formula.children.at(1)) + ")";
  }
  return "?";
}

// ---------------------------------------------------------------------------
// 规约文件：作用域 → 允许的信号/参数目录
// ---------------------------------------------------------------------------

struct ScopeCatalog {
  std::set<std::string> signals;
  std::set<std::string> parameters;
};

// 目录是规约文件与 stl_fleet_monitor.cpp 信号提取之间的契约；新增信号必须
// 同时更新两处，否则规约会在加载阶段被拒绝，而不是在运行时得到全 0 信号。
const ScopeCatalog& catalog_for(ScopeKind kind) {
  static const std::set<std::string> global_parameters = {
      "horizon",
      "maximum_load_kg",
      "energy_per_cell_percent",
      "battery_safety_reserve_percent",
      "new_task_battery_threshold_percent",
      "critical_battery_threshold_percent",
      "minimum_safety_distance_cells",
  };
  static const std::map<ScopeKind, ScopeCatalog> catalogs = [] {
    std::map<ScopeKind, ScopeCatalog> result;
    ScopeCatalog order;
    order.signals = {"t", "loaded_margin", "delivered_margin"};
    order.parameters = global_parameters;
    order.parameters.insert({"release_time", "deadline", "priority"});
    result.emplace(ScopeKind::kOrder, std::move(order));

    ScopeCatalog amr;
    amr.signals = {"t",
                   "battery",
                   "load",
                   "blocked_cell_distance",
                   "boundary_margin",
                   "edge_legal",
                   "at_charging_station",
                   "moves"};
    amr.parameters = global_parameters;
    amr.parameters.insert({"payload_kg", "initial_battery"});
    result.emplace(ScopeKind::kAmr, std::move(amr));

    ScopeCatalog pair;
    pair.signals = {"t", "pair_distance", "no_edge_swap"};
    pair.parameters = global_parameters;
    result.emplace(ScopeKind::kPair, std::move(pair));

    ScopeCatalog station;
    station.signals = {"t", "occupancy"};
    station.parameters = global_parameters;
    station.parameters.insert("capacity");
    result.emplace(ScopeKind::kStation, std::move(station));

    ScopeCatalog dependency;
    dependency.signals = {"t", "dependent_loaded_margin", "prerequisite_delivered_margin"};
    dependency.parameters = global_parameters;
    result.emplace(ScopeKind::kDependency, std::move(dependency));
    return result;
  }();
  return catalogs.at(kind);
}

void validate_formula_names(const Formula& formula,
                            const ScopeCatalog& catalog,
                            const std::string& formula_id) {
  const auto check_parameter = [&](const std::string& name, const char* what) {
    if (!name.empty() && catalog.parameters.count(name) == 0U) {
      throw SpecificationError("STL 公式 " + formula_id + " 引用了该作用域未知的" + what +
                               ": " + name);
    }
  };
  if (formula.kind == NodeKind::kAtom) {
    if (catalog.signals.count(formula.signal) == 0U) {
      throw SpecificationError("STL 公式 " + formula_id + " 引用了该作用域未知的信号: " +
                               formula.signal);
    }
    check_parameter(formula.threshold.parameter, "阈值参数");
  }
  if (formula.kind == NodeKind::kGlobally || formula.kind == NodeKind::kEventually ||
      formula.kind == NodeKind::kUntil) {
    check_parameter(formula.interval.lower.parameter, "区间参数");
    check_parameter(formula.interval.upper.parameter, "区间参数");
  }
  for (const auto& child : formula.children) {
    validate_formula_names(child, catalog, formula_id);
  }
}

ScopeKind scope_from_name(const std::string& name) {
  if (name == "order") return ScopeKind::kOrder;
  if (name == "amr") return ScopeKind::kAmr;
  if (name == "pair") return ScopeKind::kPair;
  if (name == "station") return ScopeKind::kStation;
  if (name == "dependency") return ScopeKind::kDependency;
  throw SpecificationError("STL 公式作用域未知: " + name);
}

const json::Value::Object& object_or_throw(const json::Value& value, const std::string& context) {
  if (!value.is_object()) throw SpecificationError(context + " 必须是 JSON 对象");
  return value.as_object();
}

const json::Value& required(const json::Value::Object& object,
                            const std::string& name,
                            const std::string& context) {
  const auto it = object.find(name);
  if (it == object.end()) throw SpecificationError(context + " 缺少字段: " + name);
  return it->second;
}

std::string string_or_throw(const json::Value& value, const std::string& context) {
  if (!value.is_string()) throw SpecificationError(context + " 必须是字符串");
  return value.as_string();
}

void reject_unknown_keys(const json::Value::Object& object,
                         std::initializer_list<std::string_view> allowed,
                         const std::string& context) {
  std::set<std::string> permitted;
  for (const auto key : allowed) permitted.emplace(key);
  for (const auto& entry : object) {
    if (permitted.count(entry.first) == 0U) {
      throw SpecificationError(context + " 含未知字段: " + entry.first);
    }
  }
}

// ---------------------------------------------------------------------------
// 求值：每个子公式在每个时刻同时保留布尔值、鲁棒度和决定该值的时刻
// ---------------------------------------------------------------------------

struct Series {
  std::vector<double> rho;
  std::vector<char> sat;
  std::vector<int> witness;

  explicit Series(std::size_t length) : rho(length, 0.0), sat(length, 0), witness(length, -1) {}
};

struct ResolvedInterval {
  long long lower{0};
  bool unbounded{true};
  long long upper{0};
};

long long resolve_bound(const BoundExpression& bound, const SignalTrace& trace) {
  if (bound.parameter.empty()) return bound.offset;
  const auto it = trace.parameters.find(bound.parameter);
  if (it == trace.parameters.end()) {
    throw SpecificationError("STL 区间参数在当前作用域没有取值: " + bound.parameter);
  }
  const double value = it->second;
  if (!std::isfinite(value) || std::trunc(value) != value) {
    throw SpecificationError("STL 区间参数必须是整数: " + bound.parameter);
  }
  return static_cast<long long>(value) + bound.offset;
}

ResolvedInterval resolve_interval(const Interval& interval, const SignalTrace& trace) {
  ResolvedInterval resolved;
  if (!interval.explicit_interval) return resolved;
  // 负的下界指向轨迹起点之前的过去；有限轨迹只从 start_time 开始观测，因此
  // 截断到 0（例如订单 release_time 早于 start_time 时时间窗从起点算起）。
  resolved.lower = std::max<long long>(0, resolve_bound(interval.lower, trace));
  if (interval.upper.infinite) {
    resolved.unbounded = true;
  } else {
    resolved.unbounded = false;
    resolved.upper = resolve_bound(interval.upper, trace);
  }
  return resolved;
}

double resolve_threshold(const Threshold& threshold, const SignalTrace& trace) {
  if (threshold.parameter.empty()) return threshold.literal;
  const auto it = trace.parameters.find(threshold.parameter);
  if (it == trace.parameters.end()) {
    throw SpecificationError("STL 阈值参数在当前作用域没有取值: " + threshold.parameter);
  }
  return it->second;
}

// “更弱”的比较：优先选未满足的时刻/子式，其次选鲁棒度更小者；这样违反时
// weakest_time 一定指向真实违反点，而不是某个恰好鲁棒度为 0 的满足点。
bool weaker_for_min(bool candidate_sat, double candidate_rho, bool best_sat, double best_rho) {
  if (candidate_sat != best_sat) return !candidate_sat;
  return candidate_rho < best_rho;
}

bool stronger_for_max(bool candidate_sat, double candidate_rho, bool best_sat, double best_rho) {
  if (candidate_sat != best_sat) return candidate_sat;
  return candidate_rho > best_rho;
}

Series evaluate_node(const Formula& formula, const SignalTrace& trace, std::size_t depth) {
  if (depth > kMaxFormulaDepth) throw SpecificationError("STL 公式嵌套深度超过 64");
  const std::size_t length = trace.length;
  Series series(length);
  switch (formula.kind) {
    case NodeKind::kTrue: {
      for (std::size_t t = 0; t < length; ++t) {
        series.rho[t] = kInfinity;
        series.sat[t] = 1;
        series.witness[t] = static_cast<int>(t);
      }
      return series;
    }
    case NodeKind::kAtom: {
      const auto it = trace.signals.find(formula.signal);
      if (it == trace.signals.end()) {
        throw SpecificationError("STL 信号在当前作用域不存在: " + formula.signal);
      }
      if (it->second.size() != length) {
        throw SpecificationError("STL 信号长度与轨迹长度不一致: " + formula.signal);
      }
      const double threshold = resolve_threshold(formula.threshold, trace);
      for (std::size_t t = 0; t < length; ++t) {
        const double value = it->second[t];
        double margin = 0.0;
        bool satisfied = false;
        switch (formula.op) {
          case ComparisonOperator::kGreaterEqual:
            margin = value - threshold;
            satisfied = margin >= -kEpsilon;
            break;
          case ComparisonOperator::kLessEqual:
            margin = threshold - value;
            satisfied = margin >= -kEpsilon;
            break;
          case ComparisonOperator::kGreater:
            margin = value - threshold;
            satisfied = margin > kEpsilon;
            break;
          case ComparisonOperator::kLess:
            margin = threshold - value;
            satisfied = margin > kEpsilon;
            break;
        }
        series.rho[t] = margin;
        series.sat[t] = satisfied ? 1 : 0;
        series.witness[t] = static_cast<int>(t);
      }
      return series;
    }
    case NodeKind::kNot: {
      const Series child = evaluate_node(formula.children.at(0), trace, depth + 1);
      for (std::size_t t = 0; t < length; ++t) {
        series.rho[t] = -child.rho[t];
        series.sat[t] = child.sat[t] != 0 ? 0 : 1;
        series.witness[t] = child.witness[t];
      }
      return series;
    }
    case NodeKind::kAnd:
    case NodeKind::kOr:
    case NodeKind::kImplies: {
      Series left = evaluate_node(formula.children.at(0), trace, depth + 1);
      const Series right = evaluate_node(formula.children.at(1), trace, depth + 1);
      if (formula.kind == NodeKind::kImplies) {
        // a -> b 等价于 (not a) or b；蕴含前件的鲁棒度取反后参与取大。
        for (std::size_t t = 0; t < length; ++t) {
          left.rho[t] = -left.rho[t];
          left.sat[t] = left.sat[t] != 0 ? 0 : 1;
        }
      }
      const bool conjunction = formula.kind == NodeKind::kAnd;
      for (std::size_t t = 0; t < length; ++t) {
        const bool left_sat = left.sat[t] != 0;
        const bool right_sat = right.sat[t] != 0;
        if (conjunction) {
          series.sat[t] = (left_sat && right_sat) ? 1 : 0;
          series.rho[t] = std::min(left.rho[t], right.rho[t]);
          series.witness[t] = weaker_for_min(right_sat, right.rho[t], left_sat, left.rho[t])
                                  ? right.witness[t]
                                  : left.witness[t];
        } else {
          series.sat[t] = (left_sat || right_sat) ? 1 : 0;
          series.rho[t] = std::max(left.rho[t], right.rho[t]);
          series.witness[t] = stronger_for_max(right_sat, right.rho[t], left_sat, left.rho[t])
                                  ? right.witness[t]
                                  : left.witness[t];
        }
      }
      return series;
    }
    case NodeKind::kGlobally:
    case NodeKind::kEventually: {
      const Series child = evaluate_node(formula.children.at(0), trace, depth + 1);
      const ResolvedInterval interval = resolve_interval(formula.interval, trace);
      const bool globally = formula.kind == NodeKind::kGlobally;
      for (std::size_t t = 0; t < length; ++t) {
        const long long lower = static_cast<long long>(t) + interval.lower;
        const long long upper_limit =
            interval.unbounded ? static_cast<long long>(length) - 1
                               : std::min<long long>(static_cast<long long>(length) - 1,
                                                     static_cast<long long>(t) + interval.upper);
        if (lower > upper_limit) {
          // 有限轨迹语义：G 在空窗口上为空真，F 在空窗口上为假。
          series.rho[t] = globally ? kInfinity : -kInfinity;
          series.sat[t] = globally ? 1 : 0;
          series.witness[t] = -1;
          continue;
        }
        bool best_sat = child.sat[static_cast<std::size_t>(lower)] != 0;
        double best_rho = child.rho[static_cast<std::size_t>(lower)];
        int best_witness = child.witness[static_cast<std::size_t>(lower)];
        bool all_sat = best_sat;
        bool any_sat = best_sat;
        for (long long u = lower + 1; u <= upper_limit; ++u) {
          const auto index = static_cast<std::size_t>(u);
          const bool candidate_sat = child.sat[index] != 0;
          const double candidate_rho = child.rho[index];
          all_sat = all_sat && candidate_sat;
          any_sat = any_sat || candidate_sat;
          const bool replace = globally
                                   ? weaker_for_min(candidate_sat, candidate_rho, best_sat, best_rho)
                                   : stronger_for_max(candidate_sat, candidate_rho, best_sat, best_rho);
          if (replace) {
            best_sat = candidate_sat;
            best_rho = candidate_rho;
            best_witness = child.witness[index];
          }
        }
        series.rho[t] = best_rho;
        series.sat[t] = (globally ? all_sat : any_sat) ? 1 : 0;
        series.witness[t] = best_witness;
      }
      return series;
    }
    case NodeKind::kUntil: {
      const Series left = evaluate_node(formula.children.at(0), trace, depth + 1);
      const Series right = evaluate_node(formula.children.at(1), trace, depth + 1);
      const ResolvedInterval interval = resolve_interval(formula.interval, trace);
      for (std::size_t t = 0; t < length; ++t) {
        const long long lower = static_cast<long long>(t) + interval.lower;
        const long long upper_limit =
            interval.unbounded ? static_cast<long long>(length) - 1
                               : std::min<long long>(static_cast<long long>(length) - 1,
                                                     static_cast<long long>(t) + interval.upper);
        if (lower > upper_limit) {
          series.rho[t] = -kInfinity;
          series.sat[t] = 0;
          series.witness[t] = -1;
          continue;
        }
        // phi 必须在 [t, t') 上持续成立，即使 t' 尚未进入区间也要累计前缀。
        double phi_min = kInfinity;
        bool phi_all_sat = true;
        int phi_witness = -1;
        bool best_sat = false;
        double best_rho = -kInfinity;
        int best_witness = -1;
        bool initialized = false;
        for (long long u = static_cast<long long>(t); u <= upper_limit; ++u) {
          const auto index = static_cast<std::size_t>(u);
          if (u >= lower) {
            const bool psi_binding = right.rho[index] <= phi_min;
            const double term_rho = std::min(right.rho[index], phi_min);
            const bool term_sat = right.sat[index] != 0 && phi_all_sat;
            const int term_witness = psi_binding ? right.witness[index] : phi_witness;
            if (!initialized || stronger_for_max(term_sat, term_rho, best_sat, best_rho)) {
              initialized = true;
              best_sat = term_sat;
              best_rho = term_rho;
              best_witness = term_witness;
            }
          }
          if (left.rho[index] < phi_min) {
            phi_min = left.rho[index];
            phi_witness = left.witness[index];
          }
          phi_all_sat = phi_all_sat && left.sat[index] != 0;
        }
        series.rho[t] = best_rho;
        series.sat[t] = best_sat ? 1 : 0;
        series.witness[t] = best_witness;
      }
      return series;
    }
  }
  throw SpecificationError("STL 公式节点类型未知");
}

}  // namespace

const char* scope_kind_name(ScopeKind kind) noexcept {
  switch (kind) {
    case ScopeKind::kOrder:
      return "order";
    case ScopeKind::kAmr:
      return "amr";
    case ScopeKind::kPair:
      return "pair";
    case ScopeKind::kStation:
      return "station";
    case ScopeKind::kDependency:
      return "dependency";
  }
  return "unknown";
}

const char* enforcement_name(Enforcement enforcement) noexcept {
  return enforcement == Enforcement::kGate ? "gate" : "shadow";
}

Formula parse_formula(std::string_view text) {
  Parser parser(tokenize(text));
  return parser.parse();
}

std::string format_formula(const Formula& formula) {
  return format_node(formula);
}

Specification parse_specification(const json::Value& value) {
  const auto& root = object_or_throw(value, "STL 规约");
  reject_unknown_keys(root,
                      {"schema_version", "spec_id", "spec_version", "enforcement",
                       "charging_location_ids", "formulas", "description"},
                      "STL 规约");
  Specification specification;
  specification.schema_version = string_or_throw(required(root, "schema_version", "STL 规约"),
                                                 "schema_version");
  if (specification.schema_version != "1.0") {
    throw SpecificationError("STL 规约 schema_version 必须为 \"1.0\"");
  }
  specification.spec_id = string_or_throw(required(root, "spec_id", "STL 规约"), "spec_id");
  specification.spec_version =
      string_or_throw(required(root, "spec_version", "STL 规约"), "spec_version");
  if (specification.spec_id.empty() || specification.spec_version.empty()) {
    throw SpecificationError("STL 规约 spec_id/spec_version 不能为空");
  }
  const std::string enforcement =
      string_or_throw(required(root, "enforcement", "STL 规约"), "enforcement");
  if (enforcement == "gate") {
    specification.enforcement = Enforcement::kGate;
  } else if (enforcement == "shadow") {
    specification.enforcement = Enforcement::kShadow;
  } else {
    throw SpecificationError("STL 规约 enforcement 必须为 gate 或 shadow");
  }
  if (root.count("description") != 0U) {
    (void)string_or_throw(root.at("description"), "description");
  }
  const auto charging_it = root.find("charging_location_ids");
  if (charging_it != root.end()) {
    if (!charging_it->second.is_array()) {
      throw SpecificationError("STL 规约 charging_location_ids 必须是数组");
    }
    std::set<std::string> seen;
    for (const auto& item : charging_it->second.as_array()) {
      const std::string id = string_or_throw(item, "charging_location_ids 项");
      if (id.empty() || !seen.insert(id).second) {
        throw SpecificationError("STL 规约 charging_location_ids 不能为空或重复");
      }
      specification.charging_location_ids.push_back(id);
    }
  }

  const auto& formulas = required(root, "formulas", "STL 规约");
  if (!formulas.is_array() || formulas.as_array().empty()) {
    throw SpecificationError("STL 规约 formulas 必须是非空数组");
  }
  std::set<std::string> ids;
  for (const auto& item : formulas.as_array()) {
    const auto& object = object_or_throw(item, "STL 公式");
    reject_unknown_keys(object,
                        {"id", "scope", "description", "formula", "rule_codes", "warn_below"},
                        "STL 公式");
    FormulaSpec spec;
    spec.id = string_or_throw(required(object, "id", "STL 公式"), "id");
    if (spec.id.empty() || !ids.insert(spec.id).second) {
      throw SpecificationError("STL 公式 id 不能为空或重复: " + spec.id);
    }
    spec.scope = scope_from_name(string_or_throw(required(object, "scope", "STL 公式"), "scope"));
    spec.description =
        string_or_throw(required(object, "description", "STL 公式 " + spec.id), "description");
    spec.formula_text =
        string_or_throw(required(object, "formula", "STL 公式 " + spec.id), "formula");
    spec.formula = parse_formula(spec.formula_text);
    validate_formula_names(spec.formula, catalog_for(spec.scope), spec.id);
    const auto& codes = required(object, "rule_codes", "STL 公式 " + spec.id);
    if (!codes.is_array()) throw SpecificationError("STL 公式 rule_codes 必须是数组");
    std::set<std::string> seen_codes;
    for (const auto& code : codes.as_array()) {
      const std::string text = string_or_throw(code, "rule_codes 项");
      if (text.empty() || !seen_codes.insert(text).second) {
        throw SpecificationError("STL 公式 " + spec.id + " 的 rule_codes 不能为空或重复");
      }
      spec.rule_codes.push_back(text);
    }
    const auto warn_it = object.find("warn_below");
    if (warn_it != object.end() && !warn_it->second.is_null()) {
      if (!warn_it->second.is_number() || !std::isfinite(warn_it->second.as_number())) {
        throw SpecificationError("STL 公式 " + spec.id + " 的 warn_below 必须是有限数或 null");
      }
      spec.warn_below = warn_it->second.as_number();
    }
    specification.formulas.push_back(std::move(spec));
  }
  return specification;
}

Specification parse_specification_text(std::string_view text) {
  try {
    return parse_specification(json::parse(text));
  } catch (const json::ParseError& error) {
    throw SpecificationError(std::string("STL 规约 JSON 非法: ") + error.what());
  }
}

json::Value specification_to_value(const Specification& specification) {
  json::Value::Array formulas;
  for (const auto& spec : specification.formulas) {
    json::Value::Array codes;
    for (const auto& code : spec.rule_codes) codes.emplace_back(code);
    formulas.emplace_back(json::Value::Object{
        {"description", json::Value(spec.description)},
        {"formula", json::Value(spec.formula_text)},
        {"id", json::Value(spec.id)},
        {"normalized_formula", json::Value(format_formula(spec.formula))},
        {"rule_codes", json::Value(std::move(codes))},
        {"scope", json::Value(scope_kind_name(spec.scope))},
        {"warn_below", spec.warn_below.has_value() ? json::Value(*spec.warn_below)
                                                   : json::Value(nullptr)},
    });
  }
  json::Value::Array charging;
  for (const auto& id : specification.charging_location_ids) charging.emplace_back(id);
  return json::Value::Object{
      {"charging_location_ids", json::Value(std::move(charging))},
      {"enforcement", json::Value(enforcement_name(specification.enforcement))},
      {"formula_count", json::Value(static_cast<double>(specification.formulas.size()))},
      {"formulas", json::Value(std::move(formulas))},
      {"schema_version", json::Value(specification.schema_version)},
      {"spec_id", json::Value(specification.spec_id)},
      {"spec_version", json::Value(specification.spec_version)},
  };
}

Evaluation evaluate(const Formula& formula, const SignalTrace& trace) {
  if (trace.length == 0) throw SpecificationError("STL 轨迹长度不能为 0");
  for (const auto& entry : trace.signals) {
    if (entry.second.size() != trace.length) {
      throw SpecificationError("STL 信号长度与轨迹长度不一致: " + entry.first);
    }
  }
  const Series series = evaluate_node(formula, trace, 0);
  Evaluation evaluation;
  evaluation.satisfied = series.sat[0] != 0;
  evaluation.robustness = series.rho[0];
  evaluation.vacuous = !std::isfinite(series.rho[0]);
  if (series.witness[0] >= 0) {
    evaluation.weakest_time = trace.start_time + series.witness[0];
  }
  return evaluation;
}

}  // namespace amr::planner::stl
