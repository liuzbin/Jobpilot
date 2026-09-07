"""
把"轻量模型客户端"和"重量模型客户端"做成 FastAPI 依赖，而不是在路由函数内部
直接 new 一个出来。这样测试的时候可以用 app.dependency_overrides 换成
FakeLLMClient，不需要真的配置 API Key、也不需要真的发网络请求。
"""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.llm_client import LLMClient
from app.core.llm_factory import ModelNotConfiguredError, build_client
from app.models.tables import ModelSlot

# 返回 None（而不是往外抛异常）是故意的：FastAPI 的依赖注入发生在路由函数体
# 执行之前，如果在这里抛 ModelNotConfiguredError，路由函数体里的 try/except
# 根本捕获不到，没法把"请先配置模型"这种提示友好地渲染回页面。所以这里把
# "没配置"这个状态原样传回去，由路由函数自己决定怎么提示用户。


def get_light_client(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> LLMClient | None:
    try:
        return build_client(db, settings, ModelSlot.LIGHT)
    except ModelNotConfiguredError:
        return None


def get_heavy_client(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> LLMClient | None:
    try:
        return build_client(db, settings, ModelSlot.HEAVY)
    except ModelNotConfiguredError:
        return None
