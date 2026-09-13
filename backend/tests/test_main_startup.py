"""
覆盖 `app/main.py` 里新增的 `_check_pdf_dependency`——本地 App 启动时自动
检查并（Windows 上）尝试自动安装 PDF 渲染依赖这一步的接线是否正确：真正
可用时完全不碰 `pdf_dependency_installer`（避免每次启动都做多余的探测/网络
调用）；不可用时会调用 `ensure_pdf_dependency`，并且把
`settings.skip_pdf_auto_install` 正确地取反传给它的 `auto_install` 参数——
这正是保证"正常使用时自动装、pytest 跑测试时绝不触发真实网络请求和安装
程序"这条边界的关键一行，值得单独测，不能只靠"测试没有意外变慢/挂起"这种
间接信号去信任它。
"""

from __future__ import annotations

from app import main as app_main
from app.services import resume_pdf


def test_check_pdf_dependency_noop_when_weasyprint_available(monkeypatch):
    monkeypatch.setattr(resume_pdf, "_WeasyPrintHTML", object())

    called = []
    monkeypatch.setattr(
        app_main.pdf_dependency_installer,
        "ensure_pdf_dependency",
        lambda **kwargs: called.append(kwargs) or (True, "不应该走到这里"),
    )

    settings = app_main.get_settings()
    app_main._check_pdf_dependency(settings)

    assert called == []


def test_check_pdf_dependency_calls_installer_with_inverted_skip_flag(monkeypatch):
    monkeypatch.setattr(resume_pdf, "_WeasyPrintHTML", None)

    captured = {}

    def _fake_ensure(auto_install):
        captured["auto_install"] = auto_install
        return False, "测试用消息"

    monkeypatch.setattr(app_main.pdf_dependency_installer, "ensure_pdf_dependency", _fake_ensure)

    settings = app_main.get_settings()
    settings.skip_pdf_auto_install = True
    app_main._check_pdf_dependency(settings)
    assert captured["auto_install"] is False

    settings.skip_pdf_auto_install = False
    app_main._check_pdf_dependency(settings)
    assert captured["auto_install"] is True


def test_app_startup_via_testclient_does_not_touch_real_installer(monkeypatch):
    """回归防线：正常场景下（当前测试环境里真实 WeasyPrint 是可用的），走
    完整的 TestClient 生命周期不应该触发任何安装逻辑；同时确认
    conftest.py 里 `JOBPILOT_SKIP_PDF_AUTO_INSTALL=1` 确实生效，即使万一
    某个环境下 WeasyPrint 恰好不可用，也绝不会真的发起网络请求。"""
    from fastapi.testclient import TestClient

    settings = app_main.get_settings()
    assert settings.skip_pdf_auto_install is True  # 由 conftest.py 的 isolated_home 保证

    network_touched = []
    monkeypatch.setattr(
        app_main.pdf_dependency_installer,
        "auto_install_gtk_runtime",
        lambda: network_touched.append(1),
    )

    with TestClient(app_main.app) as client:
        response = client.get("/")
        assert response.status_code in (200, 404)  # 只关心启动流程不崩、不联网

    assert network_touched == []
