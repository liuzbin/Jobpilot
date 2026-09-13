"""
定位"数据文件"（Jinja2 模板目录、alembic 迁移脚本目录）在磁盘上的真实位置。

背景（Phase 5"本地 App 打包分发"）：这几个模块此前都是直接写
`Path(__file__).resolve().parents[N]`，在"源码直接用 `python -m app.main`
跑"这种场景下没问题——但这个本地 App 最终是要打包成一个不需要用户自己装
Python/pip 的独立可执行文件分发给用户的（见 `backend/packaging/`），用
PyInstaller 打包成单文件模式之后，纯 Python 模块被塞进一个内部归档
（PYZ），这些模块的 `__file__` 不再对应磁盘上的真实文件；模板、alembic
脚本这类非 `.py` 的"数据文件"也不会跟着放在源码原来的相对位置，而是被
PyInstaller 在运行时解压到一个临时目录，这个目录只能通过 `sys._MEIPASS`
拿到，跟 `Path(__file__).resolve()` 算出来的位置完全对不上。

`app_root()` 把这个差异统一封装掉：

- 没打包（直接跑源码）时：返回 `backend/` 目录——和以前几处
  `Path(__file__).resolve().parents[N]` 算出来的基准位置完全一致，行为不变。
- 打包成单文件（onefile）时：返回 `sys._MEIPASS`，PyInstaller 运行时会把
  spec 文件里 `datas` 声明的文件按原始相对路径解压到这里。
- 打包成目录模式（onedir）时：`sys._MEIPASS` 不存在，退化成
  `sys.executable` 所在目录——本项目目前用 onefile 模式分发（见
  `backend/packaging/jobpilot.spec`），留这个分支只是不让这个模块的正确性
  绑死在"以后打包方式不会变"这个假设上。

调用方统一用 `app_root() / "app" / "templates" / ...` 这种相对 `backend/`
目录的写法，不用关心当前是不是打包状态；只要 PyInstaller 的 spec 文件把
`app/templates`、`alembic` 这两个目录按同样的相对结构放进 `datas`，两种
场景下这个函数返回的路径拼出来就是同一份文件。
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的可执行文件里（而不是直接跑源码）。
    PyInstaller 会在运行时给 `sys` 模块加上 `frozen = True` 这个属性，
    这是官方文档推荐的判断方式。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """未打包时是仓库里的 `backend/` 目录；打包后是 PyInstaller 解压/安装
    数据文件的目录（onefile 模式下是 `sys._MEIPASS`，onedir 模式下退化成
    可执行文件所在目录）。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    # 这个文件是 backend/app/core/paths.py：
    # parents[0]=core, parents[1]=app, parents[2]=backend。
    return Path(__file__).resolve().parents[2]
