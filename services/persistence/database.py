"""SQLAlchemy Engine 与事务会话工厂。

本模块只管理数据库连接生命周期，不创建表、不自动迁移，也不包含业务事务。
迁移由 Alembic 显式执行，业务事务由 Service 层统一划定。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from services.config.settings import DatabaseSettings


SessionFactory = sessionmaker[Session]


def normalise_postgres_dsn(dsn: str) -> str:
    """显式选择 psycopg 3 驱动，避免 SQLAlchemy 回退到未安装的 psycopg2。"""

    if dsn.startswith("postgresql+psycopg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    raise ValueError("PostgreSQL DSN 必须使用 postgresql:// 或 postgresql+psycopg://")


def make_session_factory(engine: Engine) -> SessionFactory:
    """创建不在提交后过期的同步会话，便于 Service 安全映射返回 DTO。"""

    return sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )


@dataclass(frozen=True, slots=True)
class DatabaseRuntime:
    """API 生命周期持有的 Engine 与会话工厂。"""

    engine: Engine
    session_factory: SessionFactory

    def dispose(self) -> None:
        """关闭连接池；不会删除表、数据或数据库。"""

        self.engine.dispose()


def create_database_runtime(settings: DatabaseSettings) -> DatabaseRuntime:
    """从保密 DSN 创建惰性连接池；首次 SQL 前不会主动联网。"""

    dsn = normalise_postgres_dsn(settings.postgres_dsn.get_secret_value())
    engine = create_engine(
        dsn,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    return DatabaseRuntime(engine=engine, session_factory=make_session_factory(engine))


__all__ = [
    "DatabaseRuntime",
    "SessionFactory",
    "create_database_runtime",
    "make_session_factory",
    "normalise_postgres_dsn",
]
