"""Whitelisted tool contracts and lazy P0-12 registry exports.

仅在真正请求注册表时导入 RAG、仿真和适配器模块，避免 P0-04 的领域契约导入
路径因为 Qdrant/Embedding 或 C++ 构建产物不可用而失去纯数据校验能力。
"""

from agent.tools.contracts import (
    TOOL_ARGUMENT_POLICIES,
    ToolError,
    ToolErrorCategory,
    ToolName,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
    UserRole,
    validate_tool_arguments,
)

__all__ = [
    "TOOL_ARGUMENT_POLICIES",
    "ToolError",
    "ToolErrorCategory",
    "ToolName",
    "ToolResult",
    "ToolResultStatus",
    "ToolSpec",
    "UserRole",
    "validate_tool_arguments",
    "ToolDefinition",
    "ToolDependencies",
    "ToolExecutor",
    "ToolHandlerResponse",
    "ToolInvocationContext",
    "ToolInvocationFailure",
    "ToolRegistry",
    "UnknownToolError",
    "build_tool_registry",
    "get_tool_specs",
]


def __getattr__(name: str):
    """按需加载运行时注册表，保持 ``services.retrieval`` 的导入无副作用。"""

    lazy_names = {
        "ToolDefinition",
        "ToolDependencies",
        "ToolExecutor",
        "ToolHandlerResponse",
        "ToolInvocationContext",
        "ToolInvocationFailure",
        "ToolRegistry",
        "UnknownToolError",
        "build_tool_registry",
        "get_tool_specs",
    }
    if name in lazy_names:
        from agent.tools import registry

        value = getattr(registry, name)
        globals()[name] = value
        return value
    raise AttributeError(name)
