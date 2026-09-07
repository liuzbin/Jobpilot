from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine():
    settings = get_settings()
    # SQLite + 多线程的 FastAPI 场景需要 check_same_thread=False；
    # 本地单进程单文件数据库，不涉及跨机并发写入,风险可控。
    return create_engine(settings.db_url, connect_args={"check_same_thread": False})


_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_sessionmaker() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)
    return _SessionLocal


def reset_engine_cache() -> None:
    """测试用：切换 JOBPILOT_HOME 之后需要重建engine，否则会连到旧路径的数据库文件。"""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def get_db() -> Generator[Session, None, None]:
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
