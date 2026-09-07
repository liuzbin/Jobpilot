"""REST / WebSocket 请求的配对校验依赖。

规则（对应实施方案"本地 App 与插件基座"一节）：
- REST 请求：header 里必须带 X-JobPilot-Token，且值与本地保存的 pairing token 一致。
- WebSocket 请求：query string 里带 token 参数,同样比对。
- 两者都会校验 Origin，必须是 chrome-extension:// 开头（如果配置了具体的
  JOBPILOT_ALLOWED_ORIGIN，则必须完全匹配那一个,用于生产环境锁定到具体插件 id）。
"""

from __future__ import annotations

from fastapi import Header, HTTPException, Request, WebSocket, status

from app.core.config import Settings, get_settings
from app.core.pairing import token_matches

TOKEN_HEADER = "X-JobPilot-Token"


def _origin_allowed(origin: str | None, settings: Settings) -> bool:
    if not origin:
        return False
    if settings.allowed_origin:
        return origin == settings.allowed_origin
    return origin.startswith("chrome-extension://")


def require_paired_request(
    request: Request,
    x_jobpilot_token: str | None = Header(default=None, alias=TOKEN_HEADER),
) -> None:
    settings = get_settings()
    origin = request.headers.get("origin")
    if not _origin_allowed(origin, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="origin not allowed")
    if not token_matches(settings, x_jobpilot_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid pairing token")


async def authorize_websocket(ws: WebSocket) -> bool:
    """校验 WebSocket 握手请求。返回 True 表示允许接入,调用方应先 accept() 再进入收发循环；
    返回 False 表示调用方应该直接 close() 并 return,不要 accept。
    """
    settings = get_settings()
    origin = ws.headers.get("origin")
    token = ws.query_params.get("token")
    if not _origin_allowed(origin, settings):
        return False
    if not token_matches(settings, token):
        return False
    return True
