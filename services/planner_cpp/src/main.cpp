#include <iostream>
#include <string_view>

// CMake 正常构建时会注入项目版本；这个默认值保证单文件编译仍然可用。
#ifndef AMR_AGENT_VERSION
#define AMR_AGENT_VERSION "0.0.0"
#endif

namespace {

// 输出 JSON 而不是自由文本，后续环境检查或评测程序可以稳定解析。
int print_version() {
  std::cout << "{\"service\":\"planner_cpp\",\"version\":\""
            << AMR_AGENT_VERSION << "\",\"cxx_standard\":" << __cplusplus
            << "}\n";
  return 0;
}

int run_self_test() {
  // P0-01 只验证 C++ 工程能编译和执行，并确认已冻结的最小场景常量。
  // 真正的 Hungarian/A*/Validator 将在 P0-08～P0-10 中替换这里的简单检查。
  constexpr int width = 30;
  constexpr int height = 20;
  constexpr int amr_count = 4;
  const bool valid = width > 0 && height > 0 && amr_count == 4;
  std::cout << "{\"test\":\"planner_cpp_smoke\",\"status\":\""
            << (valid ? "ok" : "failed") << "\"}\n";
  return valid ? 0 : 1;
}

}  // namespace

int main(int argc, char* argv[]) {
  // 只接受两个白名单参数，不解析任意命令或文件路径。
  if (argc == 2) {
    const std::string_view argument{argv[1]};
    if (argument == "--version") {
      return print_version();
    }
    if (argument == "--self-test") {
      return run_self_test();
    }
  }

  // 2 表示命令行使用错误，与自检失败的 1 区分开。
  std::cerr << "Usage: amr_planner_smoke --version | --self-test\n";
  return 2;
}
