"""Phase 0 验收点 1：数据库能从空目录跑通 alembic upgrade head,八张业务表都建出来。"""

from __future__ import annotations

import logging

from sqlalchemy import inspect

from app.core.config import get_settings
from app.core.db import get_engine
from app.core.migrate import run_migrations

EXPECTED_TABLES = {
    "profile_basic",
    "experience_entry",
    "experience_bullet",
    "jd_record",
    "match_score",
    "resume_version",
    "qa_bank",
    "model_config",
    "education_entry",
    "personal_project",
    "personal_project_bullet",
    "resume_template",
}


def test_migration_runs_clean_on_empty_db():
    settings = get_settings()
    assert not settings.db_path.exists()  # 确认确实是从空库开始

    run_migrations(settings)

    assert settings.db_path.exists()
    inspector = inspect(get_engine())
    tables = set(inspector.get_table_names())
    missing = EXPECTED_TABLES - tables
    assert not missing, f"缺少表: {missing}"
    assert "alembic_version" in tables


def test_migration_is_idempotent():
    """重复跑 upgrade head 不应该报错（模拟本地 App 每次启动都会自动跑一遍迁移的场景）。"""
    settings = get_settings()
    run_migrations(settings)
    run_migrations(settings)  # 第二次不应该抛异常
    inspector = inspect(get_engine())
    assert "profile_basic" in inspector.get_table_names()


def test_migration_does_not_disable_other_loggers():
    """
    回归测试：alembic/env.py 里的 fileConfig(...) 曾经因为用了默认的
    disable_existing_loggers=True，把迁移调用之前就存在的 logger（比如
    uvicorn 自己的 logger、我们 app 里的 "jobpilot" logger）静默禁用掉，
    导致本地 App 在真实环境（Windows + uvicorn）跑起来后,迁移一结束后面所有
    启动日志都消失,看起来像卡死。这里模拟同样的场景：先创建几个 logger,
    再跑一次迁移,断言它们没有被禁用。
    """
    pre_existing_loggers = [
        logging.getLogger("jobpilot"),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.error"),
    ]
    for lg in pre_existing_loggers:
        lg.disabled = False

    settings = get_settings()
    run_migrations(settings)

    for lg in pre_existing_loggers:
        assert not lg.disabled, f"logger {lg.name} 被 alembic 的 fileConfig 意外禁用了"
