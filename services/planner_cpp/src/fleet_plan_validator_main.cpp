#include "fleet_plan_validator/json_codec.hpp"

#include <iostream>
#include <string>

#ifndef AMR_AGENT_VERSION
#define AMR_AGENT_VERSION "0.0.0"
#endif

namespace {

constexpr std::size_t kMaxInputBytes = 4U * 1024U * 1024U;

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

}  // namespace

int main(int argc, char* argv[]) {
  if (argc == 2 && std::string(argv[1]) == "--version") return print_version();
  if (argc == 2 && std::string(argv[1]) == "--error-dictionary") {
    std::cout << amr::planner::json::serialize(
                     amr::planner::validator_json::error_dictionary_to_value())
              << '\n';
    return 0;
  }
  if (argc > 2 || (argc == 2 && std::string(argv[1]) != "--validate")) {
    print_error("invalid_arguments",
                "usage: fleet_plan_validator_cli [--validate | --version | --error-dictionary]");
    return 2;
  }

  try {
    const std::string input = read_stdin_bounded();
    const auto request = amr::planner::validator_json::request_from_value(
        amr::planner::json::parse(input));
    const auto result = amr::planner::validator::validate_fleet_plan(request);
    // status=invalid 仍是已处理的业务结果，必须由调用方检查 valid/status 和
    // errors；不能因为进程退出码为 0 就把 Validator 结论偷换成通过。
    std::cout << amr::planner::json::serialize(
                     amr::planner::validator_json::result_to_value(result))
              << '\n';
    return 0;
  } catch (const amr::planner::json::ParseError& error) {
    print_error("invalid_json_or_contract", error.what());
    return 2;
  } catch (const std::exception& error) {
    print_error("internal_error", error.what());
    return 3;
  }
}

