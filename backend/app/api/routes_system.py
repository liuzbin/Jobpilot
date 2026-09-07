from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.api.deps import authorize_websocket, require_paired_request
from app.core.config import get_settings
from app.core.pairing import load_or_create_token

logger = logging.getLogger("jobpilot")

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    paired_required: bool = True


@router.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """无需鉴权的存活探测接口。插件靠这个判断"本地 App 是否已经启动",
    在完成配对之前也能拿到这个信号,用来提示用户"检测到本地服务,请完成配对"。
    不返回任何敏感信息。
    """
    return HealthResponse(status="ok")


class StatusResponse(BaseModel):
    status: str
    server_time: str


@router.get("/api/status", response_model=StatusResponse, dependencies=[Depends(require_paired_request)])
def get_status() -> StatusResponse:
    """配对之后的正式状态接口,校验 token + Origin 通过才能拿到。"""
    return StatusResponse(status="ok", server_time=datetime.now(timezone.utc).isoformat())


@router.get("/pair", response_class=HTMLResponse)
def pair_page() -> HTMLResponse:
    """本地浏览器打开的一次性配对页面：展示 pairing token,供用户手动复制到插件侧边栏,
    或者由插件自身在用户点击"自动配对"时打开这个页面读取（同源,插件可以用
    chrome.scripting 读取页面文本）。这个页面只在用户主动导航过来时才会看到 token,
    不会被其他网页脚本跨域读取到。
    """
    settings = get_settings()
    token = load_or_create_token(settings)
    html = f"""
    <html>
      <head><title>JobPilot 本地配对</title></head>
      <body style="font-family: sans-serif; max-width: 640px; margin: 40px auto;">
        <h2>JobPilot 本地配对</h2>
        <p>把下面这段配对码复制到浏览器插件侧边栏的"配对"输入框里，完成后插件即可与本地 App 通信。</p>
        <pre id="token" style="background:#f0f0f0; padding:16px; font-size:16px; user-select:all;">{token}</pre>
        <p style="color:#888;">这段配对码只保存在你自己的电脑上，不要分享给任何人。</p>
      </body>
    </html>
    """
    return HTMLResponse(content=html)


@router.websocket("/ws/heartbeat")
async def ws_heartbeat(websocket: WebSocket) -> None:
    """插件后台 service worker 建立的长连接,用于心跳保活。
    协议很简单：客户端每隔一段时间发文本 "ping",服务端回 "pong:<server_time>"。
    """
    allowed = await authorize_websocket(websocket)
    if not allowed:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    try:
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text(f"pong:{datetime.now(timezone.utc).isoformat()}")
            else:
                # 心跳通道目前只约定 ping/pong,其他消息类型先原样丢弃,
                # 后续阶段（JD 抓取/自动填表）会在这条连接上扩展其他消息类型。
                logger.debug("unexpected ws message: %s", message)
    except WebSocketDisconnect:
        logger.info("extension websocket disconnected")
