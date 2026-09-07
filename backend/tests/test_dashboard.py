"""Phase 1：Dashboard 路由的集成测试。

用 app.dependency_overrides 把 get_light_client / get_heavy_client 换成
FakeLLMClient，这样不需要真实 API Key、不需要真实网络请求，就能把
"粘贴JD -> 分析 -> 出分数"这条端到端链路整个跑通，并且复用 test_scoring.py
里验证过的"打分必须确定性"这条核心断言。
"""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.api.deps_llm import get_heavy_client, get_light_client
from app.core.llm_client import FakeLLMClient
from app.main import app

FAKE_RESUME_EXTRACTION_RESPONSE = {
    "basic": {"full_name": "Alice Example", "school": "Waterloo"},
    "companies": [
        {
            "company_name": "Acme Corp",
            "positions": [
                {
                    "position_title": "Backend Engineer",
                    "project_name": "Payments",
                    "start_date": "2022-01",
                    "end_date": None,
                    "is_current": True,
                    "bullets": ["Built a payments service", "Reduced latency by 30%"],
                }
            ],
        }
    ],
}

FAKE_JD_EXTRACTION_RESPONSE = {
    "required_years": 3,
    "required_education": "Bachelor's degree",
    "required_clearance": False,
    "clearance_description": None,
    "plus_skills": ["Kubernetes"],
    "core_responsibilities": ["Build backend services"],
    "key_skills": ["Python", "SQL"],
}

FAKE_SCORING_RESPONSE = {
    "skill_fit_score": 75,
    "matched_key_skills": ["Python"],
    "meets_education_requirement": True,
    "meets_clearance_requirement": True,
    "plus_skills_matched": ["Kubernetes"],
    "strengths": ["Strong backend background"],
    "weaknesses": ["No Kubernetes production experience"],
}


def _client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8756")


def test_get_routes_render_without_auth():
    with _client() as client:
        for path in ("/dashboard/jobs", "/dashboard/profile", "/dashboard/models", "/dashboard/jobs/new"):
            r = client.get(path)
            assert r.status_code == 200, path


def test_dashboard_root_redirects_to_jobs():
    with _client() as client:
        r = client.get("/dashboard/", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert r.headers["location"] == "/dashboard/jobs"


def test_post_from_other_origin_is_rejected():
    with _client() as client:
        r = client.post(
            "/dashboard/profile",
            data={"full_name": "Mallory"},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403


def test_post_with_matching_local_origin_is_allowed():
    with _client() as client:
        r = client.post(
            "/dashboard/profile",
            data={"full_name": "Alice"},
            headers={"Origin": "http://127.0.0.1:8756"},
            follow_redirects=False,
        )
        assert r.status_code == 303


def test_profile_update_persists_fields():
    with _client() as client:
        r = client.post(
            "/dashboard/profile",
            data={"full_name": "Alice", "years_experience": "4.5", "target_title": "Backend Engineer"},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "基本信息已保存" in r.text
        assert "Alice" in r.text


def test_job_create_then_detail_page_shows_it():
    with _client() as client:
        r = client.post(
            "/dashboard/jobs",
            data={
                "company": "Acme",
                "title": "Backend Engineer",
                "location": "Toronto, ON",
                "description_raw": "We need a backend engineer with 3 years of experience.",
                "extra_meta_raw": "Toronto, ON · 1 week ago · 20 people clicked apply",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        detail_url = r.headers["location"].split("?")[0]

        detail = client.get(detail_url)
        assert detail.status_code == 200
        assert "Acme" in detail.text
        assert "未分析" in detail.text


def test_models_update_saves_config_and_encrypts_api_key():
    with _client() as client:
        r = client.post(
            "/dashboard/models/light",
            data={"base_url": "https://api.example.com/v1", "model_name": "test-model", "api_key": "sk-secret-123"},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "配置已保存" in r.text
        # 页面不应该把明文 api key 回显出来
        assert "sk-secret-123" not in r.text

        page = client.get("/dashboard/models")
        assert "已设置" in page.text


def test_models_update_unknown_slot_returns_404():
    with _client() as client:
        r = client.post(
            "/dashboard/models/turbo",
            data={"base_url": "https://api.example.com/v1", "model_name": "x", "api_key": ""},
        )
        assert r.status_code == 404


def test_analyze_without_configured_models_shows_friendly_error():
    with _client() as client:
        create = client.post(
            "/dashboard/jobs",
            data={"company": "Acme", "title": "Eng", "description_raw": "JD body text."},
            follow_redirects=False,
        )
        detail_url = create.headers["location"].split("?")[0]

        r = client.post(f"{detail_url}/analyze", follow_redirects=True)
        assert r.status_code == 200
        assert "还没配置" in r.text


def test_analyze_end_to_end_with_fake_llm_produces_deterministic_score():
    fake_light = FakeLLMClient(responses=[dict(FAKE_JD_EXTRACTION_RESPONSE)])
    fake_heavy = FakeLLMClient(responses=[dict(FAKE_SCORING_RESPONSE)])

    app.dependency_overrides[get_light_client] = lambda: fake_light
    app.dependency_overrides[get_heavy_client] = lambda: fake_heavy
    try:
        with _client() as client:
            client.post(
                "/dashboard/profile",
                data={"years_experience": "5", "education": "Bachelor's degree"},
            )
            create = client.post(
                "/dashboard/jobs",
                data={
                    "company": "Acme",
                    "title": "Backend Engineer",
                    "description_raw": "We need someone with 3 years of backend experience.",
                },
                follow_redirects=False,
            )
            detail_url = create.headers["location"].split("?")[0]

            r = client.post(f"{detail_url}/analyze", follow_redirects=True)
            assert r.status_code == 200
            assert "分析完成" in r.text
            assert "已分析" in r.text
            # skill_fit_score(75) + plus_skill_bonus(min(10, 2*1)=2) = 77
            assert "77" in r.text
    finally:
        app.dependency_overrides.pop(get_light_client, None)
        app.dependency_overrides.pop(get_heavy_client, None)


def test_analyze_missing_jd_returns_404():
    fake_light = FakeLLMClient(responses=[dict(FAKE_JD_EXTRACTION_RESPONSE)])
    fake_heavy = FakeLLMClient(responses=[dict(FAKE_SCORING_RESPONSE)])
    app.dependency_overrides[get_light_client] = lambda: fake_light
    app.dependency_overrides[get_heavy_client] = lambda: fake_heavy
    try:
        with _client() as client:
            r = client.post("/dashboard/jobs/99999/analyze")
            assert r.status_code == 404
    finally:
        app.dependency_overrides.pop(get_light_client, None)
        app.dependency_overrides.pop(get_heavy_client, None)


def test_resume_upload_with_fake_llm_merges_experience_and_fills_profile():
    fake_light = FakeLLMClient(responses=[dict(FAKE_RESUME_EXTRACTION_RESPONSE)])
    app.dependency_overrides[get_light_client] = lambda: fake_light
    try:
        with _client() as client:
            resume_bytes = b"Alice Example\nAcme Corp - Backend Engineer\nBuilt a payments service"
            r = client.post(
                "/dashboard/profile/resume",
                files={"resume_file": ("resume.txt", io.BytesIO(resume_bytes), "text/plain")},
                follow_redirects=True,
            )
            assert r.status_code == 200
            assert "解析完成" in r.text
            assert "Acme Corp" in r.text
            assert "Alice Example" in r.text
    finally:
        app.dependency_overrides.pop(get_light_client, None)


def test_resume_upload_without_file_shows_error():
    with _client() as client:
        r = client.post("/dashboard/profile/resume", follow_redirects=True)
        assert r.status_code == 200
        assert "没有选择文件" in r.text


def test_resume_upload_without_configured_light_model_shows_friendly_error():
    with _client() as client:
        resume_bytes = b"Some resume text"
        r = client.post(
            "/dashboard/profile/resume",
            files={"resume_file": ("resume.txt", io.BytesIO(resume_bytes), "text/plain")},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "还没配置" in r.text
