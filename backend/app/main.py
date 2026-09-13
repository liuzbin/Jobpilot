from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes_dashboard import router as dashboard_router
from app.api.routes_extension import router as extension_router
from app.api.routes_system import router as system_router
from app.core.config import get_settings
from app.core.migrate import run_migrations
from app.core.pairing import load_or_create_token
from app.services import pdf_dependency_installer, resume_pdf

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jobpilot")


def _check_pdf_dependency(settings) -> None:
    """启动时检查一次 PDF 渲染依赖能不能用；缺失时（目前只有 Windows 缺
    GTK3 Runtime 这一种真实场景）自动尝试安装，而不是等用户点了"生成 PDF"
    才靠一句错误提示让他自己去发现、自己去装——这是针对"PDF 渲染依赖缺失时
    原来只提示手动安装"这个反馈明确改的：依赖是必须的,就应该放进启动时的
    自动化流程里去装。`settings.skip_pdf_auto_install` 在测试环境里始终是
    True（见 tests/conftest.py），避免 pytest 意外触发真实的网络下载和外部
    安装程序。
    """
    if resume_pdf._WeasyPrintHTML is not None:
        return
    available, message = pdf_dependency_installer.ensure_pdf_dependency(
        auto_install=not settings.skip_pdf_auto_install
    )
    log = logger.info if available else logger.warning
    log("PDF 渲染依赖检查：%s", message)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.ensure_home()
    run_migrations(settings)
    load_or_create_token(settings)  # 确保 pairing token 存在,首次启动即可生成
    logger.info("JobPilot local app ready. data dir = %s", settings.home)
    _check_pdf_dependency(settings)
    yield


app = FastAPI(title="JobPilot Local App", version="0.1.0", lifespan=lifespan)
app.include_router(system_router)
app.include_router(extension_router)
app.include_router(dashboard_router)


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
