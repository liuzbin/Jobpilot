"""
覆盖 `app/services/pdf_dependency_installer.py`——针对用户明确反馈"PDF 渲染
依赖缺失时不该只提示手动安装，应该放进自动化流程里自动装"而做的自动安装
GTK3 Runtime（仅 Windows）模块。

真实的下载/以 UAC 提权方式静默安装/重启后生效这几步没办法在这个纯 Linux 的
测试环境里端到端验证（本来就只在 Windows 上发生，`_launch_elevated_and_wait`
内部直接调用 `ctypes.windll`，这个属性在非 Windows 平台上根本不存在），这里
覆盖的是能在任何平台上确定验证的部分：平台判断、探测函数的行为、
`ensure_pdf_dependency` 的状态机分支（已就绪 / 未启用自动安装 / 退避期内跳过
重试 / 自动安装成功但当次进程仍不可用 / 自动安装失败）、以及下载 URL 解析、
`_run_silent_install` 对 `_launch_elevated_and_wait` 返回值/异常的处理逻辑
（退出码非 0 / 超时 / UAC 授权框被取消 / 意外的 OSError）——这些全部通过
monkeypatch 掉真正的网络请求和 `_launch_elevated_and_wait` 本身来做，不会
真的联网、弹出任何系统弹窗，或者执行任何安装程序。
"""

from __future__ import annotations

import json

import pytest

from app.services import pdf_dependency_installer as installer


def test_probe_gtk_available_returns_false_on_non_windows(monkeypatch):
    monkeypatch.setattr(installer, "is_windows", lambda: False)
    assert installer.probe_gtk_available() is False


def test_ensure_pdf_dependency_skips_entirely_on_non_windows(monkeypatch):
    monkeypatch.setattr(installer, "is_windows", lambda: False)

    called = {"auto_install": False}
    monkeypatch.setattr(
        installer, "auto_install_gtk_runtime", lambda: called.__setitem__("auto_install", True)
    )

    available, message = installer.ensure_pdf_dependency(auto_install=True)

    assert available is False
    assert "非 Windows" in message
    assert called["auto_install"] is False  # 非 Windows 平台绝不应该尝试下载/安装


def test_ensure_pdf_dependency_already_available(monkeypatch):
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    monkeypatch.setattr(installer, "probe_gtk_available", lambda: True)

    available, message = installer.ensure_pdf_dependency(auto_install=True)

    assert available is True
    assert "已就绪" in message


def test_ensure_pdf_dependency_missing_but_auto_install_disabled(monkeypatch):
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    monkeypatch.setattr(installer, "probe_gtk_available", lambda: False)

    available, message = installer.ensure_pdf_dependency(auto_install=False)

    assert available is False
    assert "未启用自动安装" in message


def test_ensure_pdf_dependency_success_reports_restart_needed(monkeypatch, isolated_home):
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    # 第一次探测（安装前）失败，安装完之后再次探测——用一个真实场景里合理的
    # 顺序：安装前 False，安装动作本身不改变"当前进程"的探测结果（这正是
    # 这个模块存在的那个已知限制），所以这里第二次探测也应该还是 False，
    # 用来验证"装成功了但还需要重启"这个分支，而不是误判成"当次就能用"。
    monkeypatch.setattr(installer, "probe_gtk_available", lambda: False)

    install_calls = []
    monkeypatch.setattr(installer, "auto_install_gtk_runtime", lambda: install_calls.append(1))

    available, message = installer.ensure_pdf_dependency(auto_install=True)

    assert install_calls == [1]
    assert available is False
    assert "重启一次本地 App" in message


def test_ensure_pdf_dependency_success_immediately_usable(monkeypatch, isolated_home):
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    # 探测函数在"安装前"和"安装后立刻再探测一次"这两次调用里分别返回
    # False/True，模拟"这次运气好，刚装完当次进程就能用"的少见情况。
    probe_results = iter([False, True])
    monkeypatch.setattr(installer, "probe_gtk_available", lambda: next(probe_results))
    monkeypatch.setattr(installer, "auto_install_gtk_runtime", lambda: None)

    available, message = installer.ensure_pdf_dependency(auto_install=True)

    assert available is True
    assert "当次即可使用" in message


def test_ensure_pdf_dependency_install_failure_is_caught_and_reported(monkeypatch, isolated_home):
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    monkeypatch.setattr(installer, "probe_gtk_available", lambda: False)

    def _boom():
        raise installer.GtkAutoInstallError("模拟下载失败")

    monkeypatch.setattr(installer, "auto_install_gtk_runtime", _boom)

    available, message = installer.ensure_pdf_dependency(auto_install=True)

    assert available is False
    assert "模拟下载失败" in message
    assert "手动" in message  # 失败时必须给出手动安装的退路，不能什么都不说


def test_ensure_pdf_dependency_respects_backoff_after_recent_failure(monkeypatch, isolated_home):
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    monkeypatch.setattr(installer, "probe_gtk_available", lambda: False)

    def _boom():
        raise installer.GtkAutoInstallError("模拟下载失败")

    monkeypatch.setattr(installer, "auto_install_gtk_runtime", _boom)

    # 第一次调用：真的尝试了一次，失败并记录状态。
    installer.ensure_pdf_dependency(auto_install=True)

    # 第二次调用：应该直接命中退避逻辑，不再真的调用 auto_install_gtk_runtime。
    call_count = {"n": 0}

    def _should_not_be_called():
        call_count["n"] += 1
        raise installer.GtkAutoInstallError("不应该走到这里")

    monkeypatch.setattr(installer, "auto_install_gtk_runtime", _should_not_be_called)

    available, message = installer.ensure_pdf_dependency(auto_install=True)

    assert available is False
    assert call_count["n"] == 0
    assert "分钟后" in message


def test_ensure_pdf_dependency_retries_after_backoff_window_elapses(monkeypatch, isolated_home):
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    monkeypatch.setattr(installer, "probe_gtk_available", lambda: False)

    def _boom():
        raise installer.GtkAutoInstallError("模拟下载失败")

    monkeypatch.setattr(installer, "auto_install_gtk_runtime", _boom)
    installer.ensure_pdf_dependency(auto_install=True)

    # 伪造"已经过了退避期"：直接改写状态文件里的时间戳。
    state_path = installer._state_file_path()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["attempted_at"] -= installer._RETRY_BACKOFF_SECONDS + 1
    state_path.write_text(json.dumps(state), encoding="utf-8")

    call_count = {"n": 0}

    def _boom_again():
        call_count["n"] += 1
        raise installer.GtkAutoInstallError("再次模拟失败")

    monkeypatch.setattr(installer, "auto_install_gtk_runtime", _boom_again)

    installer.ensure_pdf_dependency(auto_install=True)

    assert call_count["n"] == 1  # 过了退避期之后应该真的重试了一次


def test_fetch_latest_installer_url_picks_exe_asset(monkeypatch):
    fake_release = {
        "assets": [
            {"name": "checksums.txt", "browser_download_url": "https://example.com/checksums.txt"},
            {"name": "gtk3-runtime-3.24.31-2022-01-04-ts-win64.exe",
             "browser_download_url": "https://example.com/gtk3-runtime.exe"},
        ]
    }

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(fake_release).encode("utf-8")

    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: _FakeResponse())

    url = installer._fetch_latest_installer_url()
    assert url == "https://example.com/gtk3-runtime.exe"


def test_fetch_latest_installer_url_raises_when_no_exe_asset(monkeypatch):
    fake_release = {"assets": [{"name": "checksums.txt", "browser_download_url": "https://example.com/x"}]}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(fake_release).encode("utf-8")

    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: _FakeResponse())

    with pytest.raises(installer.GtkAutoInstallError):
        installer._fetch_latest_installer_url()


def test_run_silent_install_raises_on_nonzero_exit_code(monkeypatch, tmp_path):
    fake_installer = tmp_path / "installer.exe"
    fake_installer.write_bytes(b"")

    monkeypatch.setattr(
        installer, "_launch_elevated_and_wait", lambda path, params, timeout: 1
    )

    with pytest.raises(installer.GtkAutoInstallError, match="退出码非 0"):
        installer._run_silent_install(fake_installer)


def test_run_silent_install_propagates_timeout_error(monkeypatch, tmp_path):
    fake_installer = tmp_path / "installer.exe"
    fake_installer.write_bytes(b"")

    def _boom(path, params, timeout):
        raise installer.GtkAutoInstallError("GTK3 Runtime 安装程序超时（也可能是一直在等你确认 UAC 授权框）。")

    monkeypatch.setattr(installer, "_launch_elevated_and_wait", _boom)

    with pytest.raises(installer.GtkAutoInstallError, match="超时"):
        installer._run_silent_install(fake_installer)


def test_run_silent_install_propagates_uac_cancelled_error(monkeypatch, tmp_path):
    """用户在系统弹出的 UAC 授权框里点了"否"，应该有一句能让用户看懂"是我自己
    取消了授权"的提示，而不是一句看不懂的 Windows 错误码。"""
    fake_installer = tmp_path / "installer.exe"
    fake_installer.write_bytes(b"")

    def _boom(path, params, timeout):
        raise installer.GtkAutoInstallError("需要管理员权限才能安装 GTK3 Runtime，但系统弹出的授权确认框被取消了")

    monkeypatch.setattr(installer, "_launch_elevated_and_wait", _boom)

    with pytest.raises(installer.GtkAutoInstallError, match="取消"):
        installer._run_silent_install(fake_installer)


def test_run_silent_install_succeeds_on_zero_exit_code(monkeypatch, tmp_path):
    fake_installer = tmp_path / "installer.exe"
    fake_installer.write_bytes(b"")

    calls = []
    monkeypatch.setattr(
        installer,
        "_launch_elevated_and_wait",
        lambda path, params, timeout: calls.append((path, params, timeout)) or 0,
    )

    installer._run_silent_install(fake_installer)  # 不应该抛异常
    assert calls == [(fake_installer, "/S", installer._INSTALL_TIMEOUT_SECONDS)]


def test_run_silent_install_wraps_unexpected_oserror(monkeypatch, tmp_path):
    fake_installer = tmp_path / "installer.exe"
    fake_installer.write_bytes(b"")

    def _boom(path, params, timeout):
        raise OSError("模拟启动失败")

    monkeypatch.setattr(installer, "_launch_elevated_and_wait", _boom)

    with pytest.raises(installer.GtkAutoInstallError, match="无法启动"):
        installer._run_silent_install(fake_installer)


def test_auto_install_gtk_runtime_refuses_on_non_windows(monkeypatch):
    monkeypatch.setattr(installer, "is_windows", lambda: False)
    with pytest.raises(installer.GtkAutoInstallError):
        installer.auto_install_gtk_runtime()
