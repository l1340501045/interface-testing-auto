"""Alembic 迁移环境：迁移用超级用户创建角色/schema/表/RLS，运行时用 app_runtime。"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

from app.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 迁移连接使用迁移账号；URL 对象全程不经 ConfigParser 插值，避免
# 密码中的 @ / : / % 等字符被错误转义或触发 % 插值。
settings = get_settings()
migration_url = settings.migrator_url()

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=migration_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema="app",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(migration_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # 版本表位于 app schema，须在迁移脚本运行前确保 schema 存在。
        # schema 初始化必须落在独立提交的事务中：若直接 connection.execute，
        # SQLAlchemy 会开启 autobegin 事务，context.begin_transaction() 将不再
        # 负责提交，退出 connect() 上下文时整段迁移被回滚（表现为“迁移成功却无表”）。
        with connection.begin():
            connection.execute(text("CREATE SCHEMA IF NOT EXISTS app"))
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema="app",
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
