"""Alembic 环境：从项目配置读取 PostgreSQL DSN 和 ORM 元数据。"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from services.config import load_settings
from services.persistence import Base, normalise_postgres_dsn


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DSN 只从现有类型化配置/环境变量读取；alembic.ini 不保存真实密码。
settings = load_settings()
database_url = normalise_postgres_dsn(
    settings.database.postgres_dsn.get_secret_value()
)
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """生成离线 SQL，不创建真实连接。"""

    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """使用 NullPool 执行迁移，完成后立即释放迁移连接。"""

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
