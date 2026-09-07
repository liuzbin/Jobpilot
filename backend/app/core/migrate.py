"""在本地 App 启动时自动把数据库结构升级到最新版本（相当于自动跑 alembic upgrade head）。

这样用户/开发者不需要每次更新代码后手动记得跑一遍 alembic 命令，本地 App 一启动
数据库结构就是最新的，符合"本地桌面应用"这种交付形态对易用性的要求。
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.config import Settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def run_migrations(settings: Settings) -> None:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.db_url)
    command.upgrade(cfg, "head")
