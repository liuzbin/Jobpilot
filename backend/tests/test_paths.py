"""
覆盖 `app/core/paths.py`——Phase 5"本地 App 打包分发"里用来统一解析"模板/
alembic 脚本这类数据文件到底在磁盘哪个位置"的小工具。真正打包成 PyInstaller
可执行文件之后的路径行为没法在普通 pytest 环境里触发（`sys.frozen`/
`sys._MEIPASS` 只有真的跑在打包后的进程里才会存在），这里通过 monkeypatch
模拟这两种状态来验证分支逻辑本身是对的。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.core import paths


def test_is_frozen_false_in_normal_pytest_run():
    # pytest 本身就是跑源码,不是 PyInstaller 打包后的进程,这里断言的是
    # "这个函数在正常开发环境下应该说什么",而不是随便找个环境验证一下。
    assert paths.is_frozen() is False


def test_app_root_unfrozen_points_at_backend_directory():
    root = paths.app_root()
    # backend/ 目录下应该能看到 app/ 和 alembic/ 这两个众所周知一定存在的
    # 子目录,用这个断言比单纯比较字符串路径更能抗住"仓库整体挪了个位置"
    # 这种情况,只要相对结构没变就还是对的。
    assert (root / "app").is_dir()
    assert (root / "alembic").is_dir()
    assert (root / "alembic.ini").is_file()


def test_app_root_frozen_onefile_uses_meipass(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert paths.is_frozen() is True
    assert paths.app_root() == tmp_path


def test_app_root_frozen_onedir_falls_back_to_executable_dir(monkeypatch, tmp_path):
    fake_exe = tmp_path / "JobPilot.exe"
    fake_exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    assert paths.app_root() == tmp_path


def test_resume_pdf_and_dashboard_templates_dirs_are_findable():
    """回归防线：确认改成 `app_root()` 之后, resume_pdf.py / routes_dashboard.py
    实际算出来的模板目录仍然指向真实存在、有内容的目录——如果打包相关的重构
    不小心把相对层级算错了,这条测试会直接因为 `.venv` 缺模板文件报错，而不是
    要等到真的渲染某个页面/PDF 时才发现路径不对。"""
    from app.api.routes_dashboard import TEMPLATES_DIR
    from app.services.resume_pdf import _TEMPLATES_DIR as RESUME_STYLES_DIR

    assert (TEMPLATES_DIR / "base.html").is_file()
    assert (RESUME_STYLES_DIR / "default.html").is_file()


def test_migrate_backend_root_finds_alembic_ini():
    from app.core.migrate import BACKEND_ROOT

    assert (BACKEND_ROOT / "alembic.ini").is_file()
    assert (Path(BACKEND_ROOT) / "alembic" / "versions").is_dir()
