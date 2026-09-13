"""
JobPilot 本地 App 全局配置。

设计原则：
- 所有持久化数据（SQLite 数据库、配对 token）都落在本机磁盘的一个目录下（JOBPILOT_HOME），
  不依赖任何云端服务，符合"数据只存本地"的产品定位。
- JOBPILOT_HOME 默认是用户主目录下的 .jobpilot，可以通过环境变量覆盖 —— 测试时会覆盖成
  临时目录，避免测试污染开发者本机的真实数据。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    重要：所有字段的默认值都必须通过 pydantic-settings 的环境变量解析机制在
    "每次实例化 Settings() 时"读取，不能在类定义时（模块 import 时）就用
    os.environ.get(...) 求出一个写死的默认值 —— 否则测试里用 monkeypatch 切换
    JOBPILOT_HOME 不会生效，永远读到进程第一次 import 这个模块时的旧值。
    这里统一用 model_config(env_prefix=...) + default_factory 来保证这一点。
    """

    model_config = SettingsConfigDict(env_prefix="JOBPILOT_")

    # 本地数据根目录，对应环境变量 JOBPILOT_HOME，默认 ~/.jobpilot
    home: Path = Field(default_factory=lambda: Path.home() / ".jobpilot")

    # 本地服务监听地址：只监听 127.0.0.1，绝不监听 0.0.0.0，避免被局域网内其他设备访问
    host: str = "127.0.0.1"
    port: int = 8756

    # 允许连接的 Chrome 插件 Origin。Manifest V3 的插件 Origin 形如
    # chrome-extension://<32位固定id>，具体 id 在插件打包/加载后才能确定，
    # 对应环境变量 JOBPILOT_ALLOWED_ORIGIN，未设置时退化为"只要是
    # chrome-extension:// 开头就放行"（生产阶段建议锁定成具体的插件 id）。
    allowed_origin: str | None = None

    # 是否跳过"启动时自动安装 PDF 渲染依赖（Windows 上的 GTK3 Runtime）"这个
    # 动作，对应环境变量 JOBPILOT_SKIP_PDF_AUTO_INSTALL。正常使用时必须是
    # False（这正是这个开关存在的意义：自动装好依赖，而不是报错完事）；
    # 测试套件里会显式打开它（见 tests/conftest.py），因为自动安装涉及真实
    # 的网络请求和执行外部安装程序，绝不能在跑 pytest 的时候被意外触发。
    skip_pdf_auto_install: bool = False

    @property
    def db_path(self) -> Path:
        return self.home / "jobpilot.sqlite3"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def pairing_token_path(self) -> Path:
        return self.home / "pairing_token.json"

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_home()
    return settings
