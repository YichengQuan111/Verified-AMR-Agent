"""项目统一的 structlog 配置。

业务代码只记录“事件名 + 结构化字段”，这里负责补充时间、级别、logger 名称，
并决定最终输出 JSON 还是便于本地阅读的控制台文本。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from services.config.settings import LoggingSettings


def configure_logging(settings: LoggingSettings) -> None:
    """配置日志系统，保证每个事件只占一行。"""

    # 先配置 Python 标准 logging。force=True 让测试或重复创建 App 时也能
    # 得到确定的 handler，而不会叠加多份输出。
    logging.basicConfig(
        level=getattr(logging, settings.level),
        format="%(message)s",
        stream=sys.stdout,
        force=True,
    )
    # processor 按顺序给事件字典补充上下文、级别、logger、UTC 时间和异常信息。
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Any
    # 生产/评测默认使用 JSON，便于后续写入 PostgreSQL 或生成验证报告；
    # 本地调试可以切换为不带颜色的可读文本。
    if settings.json_output:
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    # BoundLogger 允许 logger.bind(run_id=...) 后自动携带固定上下文。
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    # ProcessorFormatter 把 structlog 和第三方库的标准 logging 输出汇入同一格式。
    for handler in logging.getLogger().handlers:
        handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=shared_processors,
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    renderer,
                ],
            )
        )


def get_logger(name: str, **context: Any) -> structlog.stdlib.BoundLogger:
    """创建 logger，并一次性绑定 component、profile 等稳定上下文。"""

    return structlog.get_logger(name).bind(**context)
