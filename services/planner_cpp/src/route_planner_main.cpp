#include "route_planner/json_codec.hpp"

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
  std::cout << "{\"service\":\"route_planner\",\"version\":\""
            << AMR_AGENT_VERSION << "\",\"cxx_standard\":" << __cplusplus << "}\n";
  return 0;
}

void print_error(const std::string& code, const std::string& message) {
  std::cout << amr::planner::json::serialize(
                   amr::planner::route_json::error_to_value(code, message))
            << '\n';
}

}  // namespace

int main(int argc, char* argv[]) {
  std::string algorithm = "astar";
  if (argc == 2 && std::string(argv[1]) == "--version") return print_version();
  if (argc == 3 && std::string(argv[1]) == "--algorithm") {
    algorithm = argv[2];
  } else {
    print_error("invalid_arguments",
                "usage: route_planner_cli [--version | --algorithm astar|dijkstra]");
    return 2;
  }
  if (algorithm != "astar" && algorithm != "dijkstra") {
    print_error("invalid_arguments", "algorithm must be astar or dijkstra");
    return 2;
  }

  try {
    const std::string input = read_stdin_bounded();
    const auto request = amr::planner::route_json::request_from_value(
        amr::planner::json::parse(input));
    const auto result = algorithm == "astar"
                            ? amr::planner::plan_routes_astar(request)
                            : amr::planner::plan_routes_dijkstra(request);
    std::cout << amr::planner::json::serialize(
                     amr::planner::route_json::result_to_value(result))
              << '\n';
    return 0;
  } catch (const amr::planner::json::ParseError& error) {
    print_error("invalid_json_or_contract", error.what());
    return 2;
  } catch (const amr::planner::RouteError& error) {
    print_error(error.code(), error.what());
    return 2;
  } catch (const std::exception& error) {
    print_error("internal_error", error.what());
    return 3;
  }
}
