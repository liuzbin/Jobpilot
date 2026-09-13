"""
覆盖 `app/services/pdf_dependency_installer.py`——针对用户明确反馈"PDF 渲染
依赖缺失时不该只提示手动安装，应该放进自动化流程里自动装"而做的自动安装
GTK3 Runtime（仅 Windows）模块。

真实的下载/静默安装/重启后生效这几步没办法在这个纯 Linux 的测试环境里端到
端验证（本来就只在 Windows 上发生），这里覆盖的是能在任何平台上确定验证的
部分：平台判断、探测函数的行为、`ensure_pdf_dependency` 的状态机分支（已就
绪 / 未启用自动安装 / 退避期内跳过重试 / 自动安装成功但当次进程仍不可用 /
自动安装失败）、以及下载 URL 解析、静默安装子进程调用的参数和异常处理逻辑
——这些全部通过 monkeypatch 掉真正的网络请求和 subprocess 调用来做，不会
真的联网或者执行任何安装程序。
"""

from __future__ import annotations

import json
import subprocess

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

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=1)

    monkeypatch.setattr(installer.subprocess, "run", _fake_run)

    with pytest.raises(installer.GtkAutoInstallError, match="管理员"):
        installer._run_silent_install(fake_installer)


def test_run_silent_install_raises_on_timeout(monkeypatch, tmp_path):
    fake_installer = tmp_path / "installer.exe"
    fake_installer.write_bytes(b"")

    def _fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="installer.exe", timeout=300)

    monkeypatch.setattr(installer.subprocess, "run", _fake_run)

    with pytest.raises(installer.GtkAutoInstallError, match="超时"):
        installer._run_silent_install(fake_installer)


def test_run_silent_install_succeeds_on_zero_exit_code(monkeypatch, tmp_path):
    fake_installer = tmp_path / "installer.exe"
    fake_installer.write_bytes(b"")

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(installer.subprocess, "run", _fake_run)

    installer._run_silent_install(fake_installer)  # 不应该抛异常


def test_auto_install_gtk_runtime_refuses_on_non_windows(monkeypatch):
    monkeypatch.setattr(installer, "is_windows", lambda: False)
    with pytest.raises(installer.GtkAutoInstallError):
        installer.auto_install_gtk_runtime()
