"""Phase 3：插件专用 REST 接口 `/api/jobs`。

插件在职位详情页解析出 JD 之后一键发过来入库，这里复用 test_handshake.py 里已经
验证过的配对鉴权套路（Origin 必须是 chrome-extension:// 开头 + 正确的 pairing
token），重点覆盖：鉴权拦截、正常入库、空描述被拒绝、返回的 dashboard_url 能在
Dashboard 里查到对应记录。
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.core.db import get_sessionmaker
from app.main import app
from app.models.tables import JDRecord

FAKE_EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef"


def _extract_token(pair_page_html: str) -> str:
    match = re.search(r'<pre id="token"[^>]*>([^<]+)</pre>', pair_page_html)
    assert match, "配对页面里没有找到 token"
    return match.group(1)


def _auth_headers(client: TestClient) -> dict[str, str]:
    token = _extract_token(client.get("/pair").text)
    return {"Origin": FAKE_EXTENSION_ORIGIN, "X-JobPilot-Token": token}


def test_submit_job_rejects_missing_origin_and_token():
    with TestClient(app) as client:
        r = client.post("/api/jobs", json={"description_raw": "some jd text"})
        assert r.status_code == 403


def test_submit_job_rejects_wrong_token():
    with TestClient(app) as client:
        r = client.post(
            "/api/jobs",
            json={"description_raw": "some jd text"},
            headers={"Origin": FAKE_EXTENSION_ORIGIN, "X-JobPilot-Token": "wrong"},
        )
        assert r.status_code == 401


def test_submit_job_rejects_disallowed_origin_even_with_correct_token():
    with TestClient(app) as client:
        token = _extract_token(client.get("/pair").text)
        r = client.post(
            "/api/jobs",
            json={"description_raw": "some jd text"},
            headers={"Origin": "https://evil.example.com", "X-JobPilot-Token": token},
        )
        assert r.status_code == 403


def test_submit_job_creates_jd_record_and_returns_dashboard_url():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        r = client.post(
            "/api/jobs",
            json={
                "company": "Acme Corp",
                "title": "Backend Engineer",
                "description_raw": "We need a backend engineer with 3 years of experience.",
                "location": "Toronto, ON",
                "source_url": "https://www.linkedin.com/jobs/view/123456",
                "extra_meta_raw": "Toronto, ON · 2 weeks ago · 80 people clicked apply",
            },
            headers=headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert "jd_id" in body
        assert body["dashboard_url"].endswith(f"/dashboard/jobs/{body['jd_id']}")

        db = get_sessionmaker()()
        try:
            jd = db.get(JDRecord, body["jd_id"])
            assert jd is not None
            assert jd.company == "Acme Corp"
            assert jd.title == "Backend Engineer"
            assert jd.source_url == "https://www.linkedin.com/jobs/view/123456"
            # extra_meta_raw 走纯规则解析（不经过 LLM），应该已经在入库时算好
            assert jd.parsed_meta["applicants_clicked"] == 80
        finally:
            db.close()

        # 用同一个 TestClient（不带鉴权头，模拟用户直接在本机浏览器打开）确认
        # Dashboard 详情页确实能看到插件送进来的这条记录，走的是同一条入库逻辑。
        detail = client.get(f"/dashboard/jobs/{body['jd_id']}")
        assert detail.status_code == 200
        assert "Acme Corp" in detail.text


def test_submit_job_with_blank_description_is_rejected():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        r = client.post(
            "/api/jobs",
            json={"company": "Acme", "title": "Eng", "description_raw": "   "},
            headers=headers,
        )
        assert r.status_code == 422


def test_submit_job_fills_only_provided_fields():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        r = client.post(
            "/api/jobs",
            json={"description_raw": "Just a plain JD body with no extra fields."},
            headers=headers,
        )
        assert r.status_code == 200
        jd_id = r.json()["jd_id"]

        db = get_sessionmaker()()
        try:
            jd = db.get(JDRecord, jd_id)
            assert jd.company is None
            assert jd.title is None
            assert jd.location is None
            assert jd.source_url is None
        finally:
            db.close()
