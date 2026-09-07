"""Phase 0 验收点 2：插件<->本地App的配对握手与心跳。

覆盖：
- /api/health 无需鉴权即可探活
- /pair 页面能拿到 pairing token
- /api/status 在缺 token、错误 Origin、错误 token 时分别被拒绝,只有 token+Origin 都对时才放行
- /ws/heartbeat 的 ping/pong 在鉴权通过后可以正常工作,鉴权失败时连接被拒绝
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import app

FAKE_EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef"


def _extract_token(pair_page_html: str) -> str:
    match = re.search(r'<pre id="token"[^>]*>([^<]+)</pre>', pair_page_html)
    assert match, "配对页面里没有找到 token"
    return match.group(1)


def test_health_requires_no_auth():
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_pair_page_returns_token():
    with TestClient(app) as client:
        r = client.get("/pair")
        assert r.status_code == 200
        token = _extract_token(r.text)
        assert len(token) > 20


def test_status_rejects_missing_origin_and_token():
    with TestClient(app) as client:
        r = client.get("/api/status")
        assert r.status_code == 403  # 没有 Origin,先被 Origin 校验拦下


def test_status_rejects_wrong_token_with_valid_origin():
    with TestClient(app) as client:
        r = client.get(
            "/api/status",
            headers={"Origin": FAKE_EXTENSION_ORIGIN, "X-JobPilot-Token": "not-the-real-token"},
        )
        assert r.status_code == 401


def test_status_rejects_disallowed_origin_even_with_correct_token():
    with TestClient(app) as client:
        token = _extract_token(client.get("/pair").text)
        r = client.get(
            "/api/status",
            headers={"Origin": "https://evil.example.com", "X-JobPilot-Token": token},
        )
        assert r.status_code == 403


def test_status_succeeds_with_correct_token_and_origin():
    with TestClient(app) as client:
        token = _extract_token(client.get("/pair").text)
        r = client.get(
            "/api/status",
            headers={"Origin": FAKE_EXTENSION_ORIGIN, "X-JobPilot-Token": token},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert "server_time" in r.json()


def test_websocket_heartbeat_ping_pong_with_valid_pairing():
    with TestClient(app) as client:
        token = _extract_token(client.get("/pair").text)
        with client.websocket_connect(
            f"/ws/heartbeat?token={token}", headers={"Origin": FAKE_EXTENSION_ORIGIN}
        ) as ws:
            ws.send_text("ping")
            reply = ws.receive_text()
            assert reply.startswith("pong:")


def test_websocket_heartbeat_rejects_bad_token():
    with TestClient(app) as client:
        try:
            with client.websocket_connect(
                "/ws/heartbeat?token=totally-wrong", headers={"Origin": FAKE_EXTENSION_ORIGIN}
            ):
                assert False, "不应该握手成功"
        except Exception:
            pass  # 握手被拒绝会抛 WebSocketDisconnect,这里只关心"确实没连上"


def test_websocket_heartbeat_rejects_bad_origin():
    with TestClient(app) as client:
        token = _extract_token(client.get("/pair").text)
        try:
            with client.websocket_connect(
                f"/ws/heartbeat?token={token}", headers={"Origin": "https://evil.example.com"}
            ):
                assert False, "不应该握手成功"
        except Exception:
            pass
