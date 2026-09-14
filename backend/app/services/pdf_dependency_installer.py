"""
自动化安装 PDF 渲染依赖（WeasyPrint 需要的系统级 GTK3 Runtime，仅 Windows）。

背景 / 为什么要有这个模块：
用户反馈原来的做法（WeasyPrint 装不上系统库时只是给一条"请手动去装 GTK3"的
提示，装不好就一直报错）是个坑——"到时候谁知道 PDF 生成不了是这个玩意没装
啊"。用户明确要求：如果这个依赖是必须的，就应该把安装这件事放进自动化流程
里去做，而不是让用户自己发现问题、自己动手装、或者不装就一直报错。

这个模块就是那个自动化流程的核心：探测 -> （缺失时）自动下载并静默安装
GTK3 Runtime -> 重新探测。`app/main.py` 的 `lifespan` 在本地 App 每次启动时
都会调用一次，不需要用户额外记得去跑什么命令。

为什么只自动处理 Windows：
- WeasyPrint 在 Linux/macOS 上缺的系统库（libpango 等）只能通过 apt/brew
  装，这两者都需要 sudo/管理员交互、会改动系统级的包管理器状态，不适合程序
  自己静默执行；而且这两个平台的开发者本来就更容易自己用包管理器解决。
- Windows 上恰恰相反：GTK3 Runtime 有官方维护的独立安装包
  （tschoonj/GTK-for-Windows-Runtime-Environment-Installer），是 WeasyPrint
  自己的安装文档推荐的安装方式，支持 NSIS 的 `/S` 静默参数、不需要用户交
  互，装到默认路径之后 WeasyPrint 自己的 `ffi.py` 会在下次进程启动时通过
  `os.add_dll_directory` 自动发现（`C:\\Program Files\\GTK3-Runtime Win64\\
  bin`），不需要用户手动改 PATH。这正好是本项目实际遇到的 bug 场景（Windows
  用户，缺 libgobject-2.0-0）。

为什么自动装完之后，当次进程可能还是用不了、需要重启一次本地 App：
WeasyPrint 探测 DLL 目录的那段代码只在"模块被 import 的那一刻"运行一次
（`os.add_dll_directory` 必须在 `import weasyprint` 之前生效才有用），所以就
算这次启动过程中把 GTK3 装好了，这个进程里早于安装完成就已经 import 失败过
的 `weasyprint` 也不会因此自动变好——这是 WeasyPrint 自己的实现方式决定的，
不是这个模块能绕开的限制。所以这里的策略诚实地反映这一点：自动装好之后，
明确提示用户"重启一次本地 App"，而不是假装当次就能用；重启之后的下一次启
动，探测会直接通过，不会再触发下载安装。

为什么失败之后要有一段时间的退避（backoff），不是每次启动都重试：
如果用户的网络访问不了 GitHub，或者装的时候被杀毒软件拦了，每次启动本地
App 都重新走一遍下载超时+安装超时，会让"打不开 PDF 功能"变成"每次启动都要
多等很久"这个新问题——这同样是一种没考虑清楚就上的自动化，不比原来的手动
提示体验好多少。所以失败之后会在本地记一个时间戳，一段时间内直接跳过重试，
只提示"之前试过、失败了、可以手动装"，把重试留给用户下次显式重启或者过了
退避时间之后的下一次启动。

为什么静默安装需要主动弹出 Windows 的管理员权限确认框（UAC），没办法做到
完全无感：
GTK3 Runtime 安装程序默认会装到 `C:\\Program Files\\` 这个系统目录，它的安装
包本身在清单里就声明了"需要管理员权限"。早期版本这里用最普通的方式启动
安装程序（相当于代码里直接 `subprocess.run`，等价于双击运行），Windows 发现
"这个程序要管理员权限，但当前进程不是管理员"，会直接拒绝启动、报
`WinError 740`（`ERROR_ELEVATION_REQUIRED`），连弹窗询问都不会——这是
`CreateProcess` 系 API 的固有限制，只有 `ShellExecute` 系 API 配合 `"runas"`
verb 才能触发标准的 UAC 授权对话框（就是那个"是否允许此应用对你的设备进行
更改"的系统弹窗）。所以现在的做法改成用 `ShellExecuteExW` 以 `"runas"` 方式
启动安装程序：下载和安装参数本身仍然是全自动、静默的，用户只需要在系统弹出
的那个标准 UAC 框里点一次"是"——这一下确认没有任何办法绕开，是 Windows 自己
的安全机制决定的，允许任何程序不经用户同意就静默提权，本身就是一个安全漏洞。
"""

from __future__ import annotations

import ctypes
import json
import logging
import platform
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger("jobpilot.pdf_dependency")

_GITHUB_RELEASES_API = (
    "https://api.github.com/repos/tschoonj/"
    "GTK-for-Windows-Runtime-Environment-Installer/releases/latest"
)
_MANUAL_INSTALL_URL = (
    "https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases/latest"
)
_PROBE_DLL_NAME = "libgobject-2.0-0.dll"
_DOWNLOAD_TIMEOUT_SECONDS = 60
_INSTALL_TIMEOUT_SECONDS = 300
# 自动安装失败后，至少间隔这么久才会在下一次启动时重试，避免网络/环境有
# 问题时每次启动都卡一次下载超时。
_RETRY_BACKOFF_SECONDS = 3600


class GtkAutoInstallError(RuntimeError):
    """自动安装 GTK3 Runtime 失败时抛出。调用方（`ensure_pdf_dependency`）
    负责捕获并降级成一句给用户看的提示，不应该让这个异常拖垮 App 启动。"""


def is_windows() -> bool:
    return platform.system() == "Windows"


def probe_gtk_available() -> bool:
    """探测系统里能不能直接加载 GObject 的 DLL。用这个而不是真的
    `import weasyprint`，是因为 WeasyPrint 探测失败时会自己往 stderr 打印一
    大段排障提示，不适合在这种轻量探测阶段就弹出来打扰用户。"""
    if not is_windows():
        return False
    try:
        ctypes.WinDLL(_PROBE_DLL_NAME)  # type: ignore[attr-defined]
        return True
    except OSError:
        return False


def _state_file_path() -> Path:
    # 延迟 import，避免这个模块在非 Windows/测试环境下也强制拉起完整的
    # settings 初始化逻辑（虽然目前也没什么副作用，但保持这个模块尽量独立、
    # 方便单测直接构造临时目录来验证退避逻辑）。
    from app.core.config import get_settings

    return get_settings().home / "gtk3_install_state.json"


def _read_last_attempt(state_path: Path) -> dict | None:
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_attempt_state(state_path: Path, *, succeeded: bool, message: str) -> None:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"succeeded": succeeded, "message": message, "attempted_at": time.time()}),
            encoding="utf-8",
        )
    except OSError:
        # 状态记录只是用来做重试退避的优化，写失败不应该影响本次安装结果的
        # 上报——大不了下次启动多重试一次。
        logger.warning("记录 GTK3 安装尝试状态失败（不影响本次结果）", exc_info=True)


def _fetch_latest_installer_url() -> str:
    request = urllib.request.Request(
        _GITHUB_RELEASES_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "JobPilot-Setup"},
    )
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as resp:  # noqa: S310 - 固定域名
        release = json.loads(resp.read().decode("utf-8"))
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.lower().endswith(".exe"):
            return asset["browser_download_url"]
    raise GtkAutoInstallError("在 GitHub 最新 release 里没找到 .exe 安装包，可能是上游改了发布方式。")


def _download_installer(url: str, dest_dir: Path) -> Path:
    dest_path = dest_dir / "gtk3-runtime-installer.exe"
    try:
        urllib.request.urlretrieve(url, dest_path)  # noqa: S310 - 固定 github release 资产地址
    except urllib.error.URLError as exc:
        raise GtkAutoInstallError(f"下载 GTK3 Runtime 安装包失败：{exc}") from exc
    return dest_path


# Windows ShellExecuteExW 相关常量，用纯 ctypes 基础类型定义结构体字段（不用
# `ctypes.wintypes`），这样这个模块在非 Windows 平台上也能正常 import——
# 下面这些常量和结构体定义本身只是数值/内存布局声明，不涉及任何系统调用，
# 真正会在非 Windows 平台上报错的是 `ctypes.windll`，而那只在 `_launch_
# elevated_and_wait` 函数体内部被引用，只要不实际调用这个函数就不会触发。
_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SW_SHOWNORMAL = 1
_WAIT_TIMEOUT = 0x00000102
# ShellExecuteExW 失败时，用户在 UAC 授权框里点"否"对应的 GetLastError 值。
_ERROR_CANCELLED = 1223


class _ShellExecuteInfoW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hKeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIcon", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


def _launch_elevated_and_wait(executable_path: Path, parameters: str, timeout_seconds: int) -> int:
    """用 UAC 弹窗以管理员身份启动 `executable_path`，同步等待进程结束并返回
    退出码。只应该在 Windows 上被真正调用；单测里通过直接 monkeypatch 掉这个
    函数本身来验证 `_run_silent_install` 的行为分支，不在纯 Linux 环境里真的
    执行下面这些 Windows-only 的 ctypes 调用（模块顶部文档字符串"为什么静默
    安装需要主动弹出 UAC"一节解释了为什么这一步没办法完全无感）。"""
    execute_info = _ShellExecuteInfoW()
    execute_info.cbSize = ctypes.sizeof(_ShellExecuteInfoW)
    execute_info.fMask = _SEE_MASK_NOCLOSEPROCESS
    execute_info.hwnd = None
    execute_info.lpVerb = "runas"
    execute_info.lpFile = str(executable_path)
    execute_info.lpParameters = parameters
    execute_info.lpDirectory = None
    execute_info.nShow = _SW_SHOWNORMAL
    execute_info.hInstApp = None
    execute_info.lpIDList = None
    execute_info.lpClass = None
    execute_info.hKeyClass = None
    execute_info.dwHotKey = 0
    execute_info.hIcon = None
    execute_info.hProcess = None

    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    if not shell32.ShellExecuteExW(ctypes.byref(execute_info)):
        error_code = ctypes.GetLastError()  # type: ignore[attr-defined]
        if error_code == _ERROR_CANCELLED:
            raise GtkAutoInstallError(
                "需要管理员权限才能安装 GTK3 Runtime，但系统弹出的授权确认框被取消了（点了"
                "\"否\"或直接关掉了）。"
            )
        raise GtkAutoInstallError(f"无法以管理员身份启动 GTK3 Runtime 安装程序（错误码 {error_code}）。")

    if not execute_info.hProcess:
        # 极少数情况下 ShellExecuteExW 报成功但没给到进程句柄，没法等待/拿
        # 退出码，只能假定已经启动成功，不再阻塞等待安装完成。
        return 0

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    try:
        wait_result = kernel32.WaitForSingleObject(execute_info.hProcess, timeout_seconds * 1000)
        if wait_result == _WAIT_TIMEOUT:
            raise GtkAutoInstallError("GTK3 Runtime 安装程序超时（也可能是一直在等你确认 UAC 授权框）。")
        exit_code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(execute_info.hProcess, ctypes.byref(exit_code))
        return exit_code.value
    finally:
        kernel32.CloseHandle(execute_info.hProcess)


def _run_silent_install(installer_path: Path) -> None:
    try:
        exit_code = _launch_elevated_and_wait(installer_path, "/S", _INSTALL_TIMEOUT_SECONDS)
    except GtkAutoInstallError:
        raise
    except OSError as exc:
        raise GtkAutoInstallError(f"无法启动 GTK3 Runtime 安装程序：{exc}") from exc
    if exit_code != 0:
        raise GtkAutoInstallError(f"GTK3 Runtime 安装程序退出码非 0（{exit_code}）。")


def auto_install_gtk_runtime() -> None:
    """下载并静默安装 GTK3 Runtime。只应该在 `probe_gtk_available()` 返回
    False 且确认是 Windows 时调用；失败时抛 `GtkAutoInstallError`，调用方
    负责降级处理，不应该让这个失败拖垮 App 启动。"""
    if not is_windows():
        raise GtkAutoInstallError("自动安装 GTK3 Runtime 只支持 Windows。")
    installer_url = _fetch_latest_installer_url()
    import tempfile

    with tempfile.TemporaryDirectory(prefix="jobpilot-gtk3-") as tmp_dir:
        installer_path = _download_installer(installer_url, Path(tmp_dir))
        _run_silent_install(installer_path)


def ensure_pdf_dependency(auto_install: bool = True) -> tuple[bool, str]:
    """App 启动时调用一次：探测 -> （允许自动安装且不在退避期内时）尝试自动
    装 -> 返回 (当前进程内 PDF 渲染是否可用, 给用户看的状态说明)。

    注意"当前进程内是否可用"和"系统里装没装好"是两回事：即使这次自动安装
    成功了，WeasyPrint 探测 DLL 目录的逻辑只在 import 时跑一次，当前进程里
    `resume_pdf` 模块早于这次安装完成就已经 import 失败过，所以返回值里
    `available` 在"这次刚装完"的场景下仍然是 False——调用方（`app/main.py`）
    应该把第二个字符串里"需要重启一次"的提示展示给用户，不能把这个
    `available=False` 直接当成"装失败了"来理解。
    """
    if not is_windows():
        return False, "非 Windows 平台，跳过 GTK3 自动安装（PDF 渲染依赖请通过系统包管理器安装）。"

    if probe_gtk_available():
        return True, "GTK3 Runtime 已就绪，PDF 生成功能可用。"

    if not auto_install:
        return False, "缺少 GTK3 Runtime，且当前未启用自动安装。"

    state_path = _state_file_path()
    last_attempt = _read_last_attempt(state_path)
    if last_attempt and not last_attempt.get("succeeded"):
        elapsed = time.time() - float(last_attempt.get("attempted_at", 0))
        if elapsed < _RETRY_BACKOFF_SECONDS:
            retry_in_minutes = max(1, int((_RETRY_BACKOFF_SECONDS - elapsed) / 60))
            return False, (
                f"此前自动安装 GTK3 Runtime 失败过（{last_attempt.get('message', '')}），"
                f"约 {retry_in_minutes} 分钟后重启本地 App 会自动重试；也可以现在手动安装："
                f"{_MANUAL_INSTALL_URL}"
            )

    logger.info(
        "检测到缺少 PDF 渲染依赖（GTK3 Runtime），正在自动下载……下载完成后会弹出 Windows "
        "系统自己的管理员权限确认框（\"是否允许此应用对你的设备进行更改\"），请点\"是\"以继续"
        "静默安装——这一步是 Windows 的安全机制决定的，没办法做到完全无感。"
    )
    try:
        auto_install_gtk_runtime()
    except GtkAutoInstallError as exc:
        message = (
            f"自动安装 GTK3 Runtime 失败（{exc}）。可以手动下载安装：{_MANUAL_INSTALL_URL}，"
            "装好后重启本地 App 即可；PDF 生成之外的功能不受影响。"
        )
        logger.warning("自动安装 GTK3 Runtime 失败：%s", exc)
        _write_attempt_state(state_path, succeeded=False, message=str(exc))
        return False, message

    # 静默安装程序退出码正常（`auto_install_gtk_runtime` 没抛异常），说明
    # GTK3 Runtime 在系统层面已经装好了；接下来这次探测大概率仍然会失败——
    # 这不是安装失败，而是 WeasyPrint/ctypes 加载 DLL 依赖的目录搜索路径只
    # 在"进程刚启动、还没 import 任何东西"的那一刻由 WeasyPrint 自己的
    # `ffi.py` 设置一次，当前这个已经在运行中的进程看不到刚装好的目录，必须
    # 等下一次全新进程（也就是重启一次本地 App）才能生效。所以这里绝不能把
    # "这次探测还是 False"当成安装失败来报——那正是本模块文档字符串里强调
    # 的、必须诚实反映给用户的已知限制，误报成失败只会让用户又走一遍手动
    # 安装的弯路，白白重复一次本来已经成功的操作。
    if probe_gtk_available():
        message = "GTK3 Runtime 已自动安装完成，当次即可使用 PDF 生成功能。"
        logger.info(message)
        _write_attempt_state(state_path, succeeded=True, message="installed and immediately usable")
        return True, message

    message = "GTK3 Runtime 已自动安装完成，请重启一次本地 App 以启用 PDF 生成功能。"
    logger.info(message)
    _write_attempt_state(state_path, succeeded=True, message="installed, restart required")
    return False, message
