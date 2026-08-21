"""P0-06 应用层稳定异常。

Router 只把这些异常翻译为 HTTP 状态码，不暴露 SQLAlchemy、psycopg 或数据库
约束名称。这样数据库实现变化不会改变 API 的错误契约。
"""

from __future__ import annotations


class ApplicationError(Exception):
    """所有可安全返回给调用方的应用错误基类。"""

    code = "APPLICATION_ERROR"
    status_code = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ResourceNotFoundError(ApplicationError):
    """请求的运行、计划、审批或文档不存在。"""

    code = "RESOURCE_NOT_FOUND"
    status_code = 404


class PersistenceConflictError(ApplicationError):
    """唯一键、版本或并发写入冲突；底层事务已经回滚。"""

    code = "PERSISTENCE_CONFLICT"
    status_code = 409


class InvalidOperationError(ApplicationError):
    """资源存在，但当前状态不允许执行请求的操作。"""

    code = "INVALID_OPERATION"
    status_code = 409


class InvalidDocumentError(ApplicationError):
    """文档内容、文件名或访问范围不符合上传边界。"""

    code = "INVALID_DOCUMENT"
    status_code = 400


class DocumentTooLargeError(InvalidDocumentError):
    """文档超过 P0-06 的数据库内存储上限。"""

    code = "DOCUMENT_TOO_LARGE"
    status_code = 413


class DocumentAccessDeniedError(ApplicationError):
    """文档 ACL 拒绝访问；对外伪装为 404，避免泄漏文档是否存在。"""

    code = "DOCUMENT_NOT_FOUND"
    status_code = 404


__all__ = [
    "ApplicationError",
    "DocumentTooLargeError",
    "DocumentAccessDeniedError",
    "InvalidDocumentError",
    "InvalidOperationError",
    "PersistenceConflictError",
    "ResourceNotFoundError",
]
