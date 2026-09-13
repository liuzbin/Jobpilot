"""
每个测试用例都用一个独立的临时 JOBPILOT_HOME,避免：
1) 污染开发者本机 ~/.jobpilot 下的真实数据；
2) 测试之间互相残留状态（比如上一条用例生成的 pairing token 被下一条误用）。

get_settings() 用了 lru_cache,db.py 里的 engine/sessionmaker 也是模块级缓存,
所以每个用例开始前都要显式清缓存,否则切换 JOBPILOT_HOME 不会生效。
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.core.db import get_engine, get_sessionmaker, reset_engine_cache
from app.core.migrate import run_migrations


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBPILOT_HOME", str(tmp_path / "jobpilot_home"))
    # 自动安装 PDF 渲染依赖（GTK3 Runtime）涉及真实网络请求和执行外部安装
    # 程序，测试环境必须始终关掉，只允许探测逻辑跑，不允许真的触发安装。
    monkeypatch.setenv("JOBPILOT_SKIP_PDF_AUTO_INSTALL", "1")
    get_settings.cache_clear()
    reset_engine_cache()
    yield
    get_settings.cache_clear()
    reset_engine_cache()


@pytest.fixture
def db_session(isolated_home):
    """给不经过 FastAPI TestClient、只测 service 层函数的用例用：
    跑一遍迁移建好表，返回一个可以直接操作的 Session。
    """
    settings = get_settings()
    run_migrations(settings)
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
        get_engine().dispose()
