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


def require_local_browser(request: Request) -> None:
    """Dashboard 路由用的校验：Dashboard 是用户直接在自己电脑的浏览器里打开的页面,
    不经过插件、不需要配对 token。但对会改数据的 POST 请求,还是要挡一道最基本的
    防线——防止用户打开的其他恶意网页用隐藏表单跨站提交到本地服务改数据。
    做法：只要 Origin 不存在（同源导航常见情况）,或者 Origin 就是本地服务自己
    （http://127.0.0.1:<port>），就放行；来自其他站点的 Origin 一律拒绝。
    """
    settings = get_settings()
    origin = request.headers.get("origin")
    if origin is None:
        return
    expected = f"http://{settings.host}:{settings.port}"
    if origin != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="请求来源不是本地 Dashboard 页面本身，已拒绝",
        )
