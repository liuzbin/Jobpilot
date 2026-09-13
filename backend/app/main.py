from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.api.routes_dashboard import router as dashboard_router
from app.api.routes_dashboard import templates as dashboard_templates
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


@app.exception_handler(Exception)
async def handle_unexpected_exception(request: Request, exc: Exception) -> Response:
    """Phase 5"整体异常处理"：这是最后一道安全网，只会接住路由自己的
    try/except 没有预料到、真正意外的错误——各个路由函数里已经写好的
    `except SomeSpecificError` 会先处理掉预期内的失败情况（模型没配置、
    JD 不存在……），不会走到这里；这里兜住的是"代码里一个没想到的 bug"，
    目的是不让用户看到一坨原始的 Python 堆栈或者浏览器自带的
    "Internal Server Error"空白页，而是一句能看懂的中文提示 + 明确告诉
    他完整错误信息记在本地 App 的日志里（终端窗口能看到），方便反馈问题。

    真实异常永远先完整记到日志（`logger.exception` 自带堆栈），返回给用户
    的只是摘要——这个本地 App 只监听 127.0.0.1、单用户使用，跟其他路由已有
    的 `except Exception as exc: ... f"失败: {exc}"` 这种直接把异常信息展示
    给用户的现有约定保持一致，不是这里单独放宽了什么。

    按路径区分响应格式：`/dashboard` 开头是给人在浏览器里看的页面，走
    Jinja2 渲染一个和其他页面观感一致的错误页；其余（插件专用的 `/api/...`
    等）是给代码消费的接口，返回 JSON，且用 `{"detail": ...}` 这个键名——
    和 FastAPI `HTTPException` 默认的错误响应形状保持一致，因为插件侧
    `service_worker.js` 已经在读 `body.detail` 这个字段展示错误,不应该为了
    "意外错误"这一种情况就单独发明一套不一样的错误响应格式。
    """
    logger.exception("未捕获的异常：%s %s", request.method, request.url.path)
    if request.url.path.startswith("/dashboard"):
        return dashboard_templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "active": None,
                "error_summary": str(exc) or exc.__class__.__name__,
            },
            status_code=500,
        )
    return JSONResponse(status_code=500, content={"detail": f"服务器内部错误：{exc}"})


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
