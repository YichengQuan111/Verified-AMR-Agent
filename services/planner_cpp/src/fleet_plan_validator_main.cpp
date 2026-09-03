#include "fleet_plan_validator/json_codec.hpp"
#include "fleet_plan_validator/stl_monitor.hpp"

#include <fstream>
#include <iostream>
#include <optional>
#include <sstream>
#include <string>

#ifndef AMR_AGENT_VERSION
#define AMR_AGENT_VERSION "0.0.0"
#endif

namespace {

constexpr std::size_t kMaxInputBytes = 4U * 1024U * 1024U;
constexpr std::size_t kMaxSpecificationBytes = 1U * 1024U * 1024U;

std::string read_stdin_bounded() {
  std::string input;
  input.reserve(64U * 1024U);
  char buffer[8192];
  while (std::cin.read(buffer, sizeof(buffer)) || std::cin.gcount() != 0) {
    input.append(buffer, static_cast<std::size_t>(std::cin.gcount()));
    if (input.size() > kMaxInputBytes) {
      throw amr::planner::json::ParseError("stdin JSON exceeds the 4 MiB safety limit");
    }
  }
  if (std::cin.bad()) {
    throw amr::planner::json::ParseError("failed to read JSON from stdin");
  }
  return input;
}

// P1-1：规约文件是唯一允许 CLI 读取的文件，路径只来自调用方 argv 的显式参数
// （Python 侧固定为仓库内 config/stl/fleet_plan_stl_spec.json），请求 JSON 中
// 没有任何字段能指定它；这样 LLM 生成的计划无法替换或削弱规约。
amr::planner::stl::Specification load_specification(const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    throw amr::planner::stl::SpecificationError("cannot open STL specification: " + path);
  }
  std::string text;
  char buffer[8192];
  while (input.read(buffer, sizeof(buffer)) || input.gcount() != 0) {
    text.append(buffer, static_cast<std::size_t>(input.gcount()));
    if (text.size() > kMaxSpecificationBytes) {
      throw amr::planner::stl::SpecificationError(
          "STL specification exceeds the 1 MiB safety limit");
    }
  }
  return amr::planner::stl::parse_specification_text(text);
}

int print_version() {
  std::cout << "{\"service\":\"fleet_plan_validator\",\"version\":\""
            << AMR_AGENT_VERSION << "\",\"cxx_standard\":" << __cplusplus << "}\n";
  return 0;
}

void print_error(const std::string& code, const std::string& message) {
  std::cout << amr::planner::json::serialize(
                   amr::planner::validator_json::error_to_value(code, message))
            << '\n';
}

int print_usage() {
  print_error("invalid_arguments",
              "usage: fleet_plan_validator_cli [--validate [--stl-spec <path>] | --version | "
              "--error-dictionary | --describe-stl-spec <path>]");
  return 2;
}

}  // namespace

int main(int argc, char* argv[]) {
  if (argc == 2 && std::string(argv[1]) == "--version") return print_version();
  if (argc == 2 && std::string(argv[1]) == "--error-dictionary") {
    std::cout << amr::planner::json::serialize(
                     amr::planner::validator_json::error_dictionary_to_value())
              << '\n';
    return 0;
  }

  bool validate = argc == 1;
  std::optional<std::string> spec_path;
  std::optional<std::string> describe_path;
  for (int index = 1; index < argc; ++index) {
    const std::string argument = argv[index];
    if (argument == "--validate" && !validate) {
      validate = true;
    } else if (argument == "--stl-spec" && index + 1 < argc && !spec_path.has_value()) {
      spec_path = argv[++index];
    } else if (argument == "--describe-stl-spec" && index + 1 < argc &&
               !describe_path.has_value() && argc == 3) {
      describe_path = argv[++index];
    } else {
      return print_usage();
    }
  }
  if (describe_path.has_value()) {
    try {
      const auto specification = load_specification(*describe_path);
      std::cout << amr::planner::json::serialize(
                       amr::planner::stl::specification_to_value(specification))
                << '\n';
      return 0;
    } catch (const amr::planner::stl::SpecificationError& error) {
      print_error("invalid_stl_specification", error.what());
      return 2;
    }
  }
  if (!validate) return print_usage();

  try {
    std::optional<amr::planner::stl::Specification> specification;
    if (spec_path.has_value()) specification = load_specification(*spec_path);
    const std::string input = read_stdin_bounded();
    const auto request = amr::planner::validator_json::request_from_value(
        amr::planner::json::parse(input));
    const auto result = amr::planner::validator::validate_fleet_plan(
        request, specification.has_value() ? &*specification : nullptr);
    // status=invalid 仍是已处理的业务结果，必须由调用方检查 valid/status 和
    // errors；不能因为进程退出码为 0 就把 Validator 结论偷换成通过。
    std::cout << amr::planner::json::serialize(
                     amr::planner::validator_json::result_to_value(result))
              << '\n';
    return 0;
  } catch (const amr::planner::stl::SpecificationError& error) {
    // 规约不可用时不能退化成“只跑规则层”，否则第二判定层会被静默绕过。
    print_error("invalid_stl_specification", error.what());
    return 2;
  } catch (const amr::planner::json::ParseError& error) {
    print_error("invalid_json_or_contract", error.what());
    return 2;
  } catch (const std::exception& error) {
    print_error("internal_error", error.what());
    return 3;
  }
}
