import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# 保证 `app` 包可以被 import 到（alembic 命令默认在 backend/ 目录下执行）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import tables  # noqa: E402,F401  # 确保所有模型都被注册到 Base.metadata

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# 数据库地址不写死在 alembic.ini 里，而是跟随 app 的 Settings（JOBPILOT_HOME 环境变量），
# 这样测试时把 JOBPILOT_HOME 指到临时目录，alembic 也会跟着操作临时目录里的数据库文件，
# 不会碰到开发者本机 ~/.jobpilot 下的真实数据。
config.set_main_option("sqlalchemy.url", get_settings().db_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
#
# 重要：这里必须传 disable_existing_loggers=False。fileConfig 默认
# disable_existing_loggers=True，会把"在这次调用之前就已经创建、但没有在
# alembic.ini 的 [loggers] 里登记"的所有 logger 静默禁用掉。因为我们是在
# FastAPI 的 lifespan 启动阶段、进程内直接调用 alembic 做迁移（而不是单独跑
# alembic 命令行），这个默认行为会把 uvicorn 自己的 logger 和我们 app 里的
# "jobpilot" logger 一起禁用，导致迁移一结束,后面所有的启动日志（包括
# "Uvicorn running on http://127.0.0.1:8756"）全部消失,看起来像卡住了,
# 实际上服务已经正常启动,只是不再打印任何日志。
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
