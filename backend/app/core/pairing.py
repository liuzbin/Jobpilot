"""
配对 token 的生成与校验。

本地 App 首次启动时生成一个随机 token 落盘保存；插件首次连接时，用户把这个 token
填进侧边栏完成"配对"，之后插件的每一次 REST/WebSocket 请求都要带上它。

这层校验存在的意义：本地服务虽然只监听 127.0.0.1，但用户浏览器里打开的任意网页
理论上也能向 localhost 端口发请求（同源策略不拦截"发请求"，只拦截"读响应"，
对于会产生副作用的请求仍然构成风险）。token + Origin 双重校验用来确保只有我们
自己的插件能跟本地 App 对话。
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from app.core.config import Settings


def load_or_create_token(settings: Settings) -> str:
    """读取本地已保存的配对 token；不存在则生成一个新的并落盘。"""
    path = settings.pairing_token_path
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        token = data.get("token")
        if token:
            return token

    token = secrets.token_urlsafe(32)
    payload = {
        "token": token,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return token


def regenerate_token(settings: Settings) -> str:
    """强制生成一个新 token（用于用户主动"解除配对/重新配对"场景）。"""
    token = secrets.token_urlsafe(32)
    payload = {
        "token": token,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    settings.pairing_token_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return token


def token_matches(settings: Settings, candidate: str | None) -> bool:
    if not candidate:
        return False
    path = settings.pairing_token_path
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    expected = data.get("token")
    if not expected:
        return False
    # 用 secrets.compare_digest 避免时序攻击（虽然是本地场景，风险很低，但成本几乎为零，顺手做好）
    return secrets.compare_digest(expected, candidate)
