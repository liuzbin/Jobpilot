"""
Phase 5"整体异常处理"：覆盖 `app/main.py` 里新增的全局兜底异常处理器
`handle_unexpected_exception`——只处理各路由自己的 try/except 没有预料到的
真正意外错误，用一个在测试运行期间临时挂上去、专门用来"制造一次没人接住的
异常"的路由来触发它,测试结束后立刻拆掉,不影响其他测试文件。

不测试"某个具体路由是不是应该有自己的 try/except"这种事——那是每个路由
自己的业务判断，各自的测试文件已经在覆盖；这里只测这道最后防线本身触发时
表现对不对：`/dashboard` 开头返回友好的 HTML 页面，其它路径返回
`{"detail": ...}` 形状的 JSON（和插件侧 `service_worker.js` 已经在读的
字段保持一致），并且真实异常信息确实进了服务器日志而不是被吞掉。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.main import app


@pytest.fixture
def temporary_boom_routes():
    """临时挂两条一定会抛未捕获异常的路由：一条在 /dashboard 前缀下（模拟
    浏览器页面场景），一条在外面（模拟插件专用 JSON 接口场景）。用完立刻
    从 app.router.routes 里摘掉，不能让它们留在这个全局单例 app 上影响
    其他测试文件。"""

    @app.get("/dashboard/__test_only_boom")
    def _boom_html():
        raise RuntimeError("模拟一个没有被任何路由自己 try/except 捕获的意外错误")

    @app.get("/__test_only_boom_json")
    def _boom_json():
        raise RuntimeError("模拟插件专用接口路径上的意外错误")

    added_paths = {"/dashboard/__test_only_boom", "/__test_only_boom_json"}
    try:
        yield
    finally:
        app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) not in added_paths]


def test_unexpected_exception_on_dashboard_path_returns_friendly_html_page(temporary_boom_routes):
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/dashboard/__test_only_boom")
        assert r.status_code == 500
        assert "出错了" in r.text
        assert "模拟一个没有被任何路由自己" in r.text
        # 不应该是浏览器默认的空白 500 页面或者原始堆栈文本
        assert "Traceback (most recent call last)" not in r.text


def test_unexpected_exception_on_non_dashboard_path_returns_detail_json(temporary_boom_routes):
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/__test_only_boom_json")
        assert r.status_code == 500
        body = r.json()
        assert "detail" in body
        assert "模拟插件专用接口路径上的意外错误" in body["detail"]


def test_unexpected_exception_is_logged_with_full_traceback(temporary_boom_routes, monkeypatch):
    """真实异常必须完整记到日志，不能被这道兜底处理器悄悄吞掉——用
    monkeypatch 直接替换 `logger.exception` 而不是用 `caplog`：TestClient
    实际上是在自己的后台线程/事件循环里跑这个 ASGI app 的，`logger.exception`
    是这个模块级单例 logger 对象上的方法，monkeypatch 换掉它之后不管请求
    实际在哪个线程处理，调用的都是同一个对象、同一个被替换过的方法，比指望
    `caplog` 的 handler 一定能在这种场景下按预期抓到记录更可靠。"""
    calls = []
    monkeypatch.setattr(app_main.logger, "exception", lambda *args, **kwargs: calls.append((args, kwargs)))

    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/dashboard/__test_only_boom")

    assert len(calls) == 1
    args, _ = calls[0]
    assert "未捕获的异常" in args[0]
    assert "/dashboard/__test_only_boom" in args[2]


def test_known_http_exceptions_are_unaffected_by_the_global_handler():
    """回归防线：全局兜底处理器只应该接住"完全没被处理过"的异常，不能
    误伤已经有专门处理逻辑的 HTTPException（比如 404）——这类响应应该
    还是原来的样子，不会被这道新加的安全网多包一层。"""
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/dashboard/jobs/999999")
        assert r.status_code == 404
        assert r.json() == {"detail": "JD not found"}
