# -*- mode: python ; coding: utf-8 -*-
"""
Phase 5"本地 App 打包分发"：把 `backend/` 打包成一个不需要用户自己装
Python/pip 的独立可执行文件。

**重要：PyInstaller 不能跨平台编译**——必须在目标系统上跑这个命令，才能
产出那个系统能用的可执行文件。也就是说要给 Windows 用户分发 .exe，就必须
在一台 Windows 机器上运行 `pyinstaller packaging/jobpilot.spec`；在 Linux/
macOS 上跑这个 spec 只会产出对应平台自己的可执行文件，验证的是"打包配置本身
对不对"（数据文件有没有正确带上、`app.core.paths.app_root()` 在打包后的进程
里能不能正确找到模板和 alembic 脚本、迁移能不能正常跑），不是给 Windows 用户
用的产物——这条边界在 `README.md`"打包成独立可执行文件"一节里也写清楚了。

用法（在目标系统上，backend/ 目录下）：
    pip install pyinstaller
    pyinstaller packaging/jobpilot.spec
产出在 `dist/JobPilot/`（Windows 上是 `dist/JobPilot/JobPilot.exe` 加同目录
下的依赖文件——用的是 onedir 模式而不是单文件 onefile 模式，原因见下面
`EXE`/`COLLECT` 之前的说明）。

数据文件（`datas`）按"发布出去之后，相对 app_root() 的目录结构"来写，跟
`app/core/paths.py` 里 `app_root() / "app" / "templates" / ...` 这种写法
对应——onedir 模式下 `app_root()` 落到可执行文件所在目录（`sys._MEIPASS`
在 onedir 下就是那个目录本身），所以这里 `datas` 的目标路径必须是
`"app/templates"`、`"alembic"` 这种相对路径，不能是绝对路径。
"""

from pathlib import Path

# `SPECPATH` 是 PyInstaller 执行 spec 文件时自动注入的内置变量,指向这个
# spec 文件自己所在的目录（`backend/packaging/`）——不用 `__file__`（spec
# 文件是被 `exec` 出来的,没有稳定的 `__file__` 语义）,也不用 `Path.cwd()`
# （取决于用户在哪个目录下敲 `pyinstaller` 命令,不可靠：实测在 backend/
# 目录下运行 `pyinstaller packaging/jobpilot.spec` 时,Analysis 里的脚本
# 路径实际是相对 spec 文件所在目录解析的,不是相对 cwd）。
BACKEND_ROOT = Path(SPECPATH).resolve().parent

block_cipher = None

a = Analysis(
    [str(BACKEND_ROOT / "app" / "main.py")],
    pathex=[str(BACKEND_ROOT)],
    binaries=[],
    datas=[
        (str(BACKEND_ROOT / "app" / "templates"), "app/templates"),
        (str(BACKEND_ROOT / "alembic"), "alembic"),
        (str(BACKEND_ROOT / "alembic.ini"), "."),
    ],
    hiddenimports=[
        # uvicorn 用字符串 "app.main:app" 延迟 import 应用对象,PyInstaller
        # 静态分析看不到这条路径,同时这几个 uvicorn 子模块也是靠内部按需
        # import 加载的,不显式声明的话经常会在打包后报 ModuleNotFoundError。
        "app.main",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        # WeasyPrint/cffi 的后端探测逻辑也是运行时按需 import 的。
        "weasyprint",
        "PIL._tkinter_finder",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JobPilot",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # 保留控制台窗口——本地 App 的运行日志（包括 PDF 渲染依赖
    # 检查结果那一行）都是普通用户唯一能看到诊断信息的地方，做成静默后台进程
    # 反而不利于本项目"出问题要让用户看得到、看得懂"这条一贯的设计取向。
)

# 用 onedir（COLLECT）而不是 onefile：onefile 模式每次启动都要先把整个包
# 解压到一个临时目录再运行,启动慢一个数量级,而且"打包后需要重启一次本地
# App 才能生效"这种场景（见 pdf_dependency_installer.py）重启会更慢；onedir
# 模式下所有文件常驻在 dist/JobPilot/ 目录里,直接运行,重启是瞬间的事。
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="JobPilot",
)
