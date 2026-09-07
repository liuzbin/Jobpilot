"""
本地加密存储 API Key。

方案说明（相对最初方案里"用 OS keyring"的一处简化,记在 docs/DEVELOPMENT_LOG.md 里）：
没有直接依赖操作系统的 keyring（macOS 钥匙串 / Windows 凭据管理器 / Linux Secret
Service），而是用对称加密（Fernet）把 API Key 加密后存在本地文件里,加密密钥单独
存一个文件，权限限制为仅当前用户可读。原因：
1) Linux 服务器/CI 环境通常没有可用的 Secret Service,keyring 库在这类环境下会
   直接抛异常或静默失败，给自动化测试和无图形界面场景带来不必要的复杂度；
2) 我们本来就要求"数据全部在本地磁盘"，用文件系统权限 + 对称加密已经能达到
   "不在数据库里明文存 API Key"这个核心诉求；
3) 后续如果要接回 OS keyring,只需要替换这个模块内部的实现,上层调用方
   （model_config 的读写逻辑）不需要改动。

存储位置：
- 密钥文件：<JOBPILOT_HOME>/secret.key
- 加密后的值：<JOBPILOT_HOME>/secrets_store.json，格式 {"<ref>": "<base64密文>"}
"""

from __future__ import annotations

import json
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings


def _key_path(settings: Settings) -> Path:
    return settings.home / "secret.key"


def _store_path(settings: Settings) -> Path:
    return settings.home / "secrets_store.json"


def _load_or_create_key(settings: Settings) -> bytes:
    path = _key_path(settings)
    if path.exists():
        return path.read_bytes()
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except (NotImplementedError, OSError):
        # Windows 上 chmod 语义不同,尽力而为即可,不应该导致启动失败
        pass
    return key


def _load_store(settings: Settings) -> dict:
    path = _store_path(settings)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_store(settings: Settings, store: dict) -> None:
    _store_path(settings).write_text(json.dumps(store, indent=2), encoding="utf-8")


def set_secret(settings: Settings, ref: str, value: str) -> None:
    """加密保存一个密钥,ref 是它在 store 里的键名（比如 "model_config:light:api_key"）。"""
    fernet = Fernet(_load_or_create_key(settings))
    store = _load_store(settings)
    store[ref] = fernet.encrypt(value.encode("utf-8")).decode("ascii")
    _save_store(settings, store)


def get_secret(settings: Settings, ref: str | None) -> str | None:
    if not ref:
        return None
    store = _load_store(settings)
    ciphertext = store.get(ref)
    if not ciphertext:
        return None
    fernet = Fernet(_load_or_create_key(settings))
    try:
        return fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return None


def delete_secret(settings: Settings, ref: str) -> None:
    store = _load_store(settings)
    if ref in store:
        del store[ref]
        _save_store(settings, store)
