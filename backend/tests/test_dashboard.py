"""Phase 1：Dashboard 路由的集成测试。

用 app.dependency_overrides 把 get_light_client / get_heavy_client 换成
FakeLLMClient，这样不需要真实 API Key、不需要真实网络请求，就能把
"粘贴JD -> 分析 -> 出分数"这条端到端链路整个跑通，并且复用 test_scoring.py
里验证过的"打分必须确定性"这条核心断言。
"""

from __future__ import annotations

import html
import io
import re

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


# ---------- Phase 2：画像深化（追问式访谈） ----------

FAKE_QUESTIONS_RESPONSE = {"questions": ["数据规模大概多大？", "你具体负责哪一层？"]}
FAKE_SYNTHESIS_RESPONSE = {"background_notes": "每日处理2TB日志，基于Hadoop生态做批处理ETL，负责数据接入与清洗层。"}


def _seed_position_via_upload(client) -> int:
    fake_light = FakeLLMClient(responses=[dict(FAKE_RESUME_EXTRACTION_RESPONSE)])
    app.dependency_overrides[get_light_client] = lambda: fake_light
    try:
        client.post(
            "/dashboard/profile/resume",
            files={"resume_file": ("resume.txt", io.BytesIO(b"resume text"), "text/plain")},
        )
    finally:
        app.dependency_overrides.pop(get_light_client, None)
    r = client.get("/dashboard/profile")
    match = re.search(r"/dashboard/profile/positions/(\d+)", r.text)
    assert match, "没有在画像页面找到项目详情链接"
    return int(match.group(1))


def test_position_detail_page_renders():
    with _client() as client:
        position_id = _seed_position_via_upload(client)
        r = client.get(f"/dashboard/profile/positions/{position_id}")
        assert r.status_code == 200
        assert "Acme Corp" in r.text


def test_position_detail_unknown_id_returns_404():
    with _client() as client:
        r = client.get("/dashboard/profile/positions/99999")
        assert r.status_code == 404


def test_interview_start_without_light_model_shows_friendly_error():
    with _client() as client:
        position_id = _seed_position_via_upload(client)
        r = client.get(f"/dashboard/profile/positions/{position_id}/interview", follow_redirects=True)
        assert r.status_code == 200
        assert "还没配置" in r.text


def test_interview_full_round_trip_updates_background_notes():
    with _client() as client:
        position_id = _seed_position_via_upload(client)

        fake_light = FakeLLMClient(responses=[dict(FAKE_QUESTIONS_RESPONSE)])
        app.dependency_overrides[get_light_client] = lambda: fake_light
        try:
            r = client.get(f"/dashboard/profile/positions/{position_id}/interview")
            assert r.status_code == 200
            assert "数据规模大概多大" in r.text
        finally:
            app.dependency_overrides.pop(get_light_client, None)

        fake_light2 = FakeLLMClient(responses=[dict(FAKE_SYNTHESIS_RESPONSE)])
        app.dependency_overrides[get_light_client] = lambda: fake_light2
        try:
            r = client.post(
                f"/dashboard/profile/positions/{position_id}/interview",
                data={
                    "question_count": "2",
                    "question_0": "数据规模大概多大？",
                    "answer_0": "2TB/天",
                    "question_1": "你具体负责哪一层？",
                    "answer_1": "接入与清洗层",
                },
                follow_redirects=True,
            )
            assert r.status_code == 200
            assert "已更新" in r.text
        finally:
            app.dependency_overrides.pop(get_light_client, None)

        r = client.get(f"/dashboard/profile/positions/{position_id}")
        assert "每日处理2TB日志" in r.text
        assert "2TB/天" in r.text  # 历史问答记录也应该展示出来


# ---------- Phase 2：简历重制（K 值 + 技能延伸建议确认） ----------


def _seed_jd_with_score(client) -> str:
    """建一条已经跑过 /analyze 的 JD，返回它的详情页 URL。"""
    fake_light = FakeLLMClient(responses=[dict(FAKE_JD_EXTRACTION_RESPONSE)])
    fake_heavy = FakeLLMClient(responses=[dict(FAKE_SCORING_RESPONSE)])
    app.dependency_overrides[get_light_client] = lambda: fake_light
    app.dependency_overrides[get_heavy_client] = lambda: fake_heavy
    try:
        create = client.post(
            "/dashboard/jobs",
            data={
                "company": "Beta Inc",
                "title": "Big Data Engineer",
                "description_raw": "We need Hadoop, Spark and RAG experience for our data platform.",
            },
            follow_redirects=False,
        )
        jd_url = create.headers["location"].split("?")[0]
        client.post(f"{jd_url}/analyze")
    finally:
        app.dependency_overrides.pop(get_light_client, None)
        app.dependency_overrides.pop(get_heavy_client, None)
    return jd_url


def test_tailor_draft_without_configured_models_shows_friendly_error():
    with _client() as client:
        jd_url = _seed_jd_with_score(client)
        r = client.get(f"{jd_url}/tailor", params={"k": 5}, follow_redirects=True)
        assert r.status_code == 200
        assert "还没配置" in r.text


def test_tailor_draft_and_confirm_full_flow():
    with _client() as client:
        position_id = _seed_position_via_upload(client)
        # 故意不走 _seed_jd_with_score（那条路径会先用 FAKE_JD_EXTRACTION_RESPONSE
        # 跑一次 /analyze，把 key_skills 缓存成 Python/SQL），这里要让 /tailor
        # 自己第一次解析 JD，用下面这份包含 Spark/RAG 的结构化结果。
        create = client.post(
            "/dashboard/jobs",
            data={
                "company": "Beta Inc",
                "title": "Big Data Engineer",
                "description_raw": "We need Hadoop, Spark and RAG experience for our data platform.",
            },
            follow_redirects=False,
        )
        jd_url = create.headers["location"].split("?")[0]

        # JD 要的两个关键词在这份画像里都没有真实证据（画像是 Payments 相关的
        # 经历），所以不会有任何"命中"，重点验证的是"延伸建议"这条链路：
        # Spark 被判定合理、RAG 被判定不合理并被过滤掉。K=10 保证两个缺失
        # 关键词都进入待延伸列表（不会被 K 值的比例选择提前刷掉）。
        jd_parse_fake = FakeLLMClient(
            responses=[
                {
                    "required_years": None,
                    "required_education": None,
                    "required_clearance": False,
                    "plus_skills": [],
                    "core_responsibilities": [],
                    "key_skills": ["Spark", "RAG"],
                }
            ]
        )
        extend_fake = FakeLLMClient(
            responses=[
                {
                    "suggestions": [
                        {
                            "keyword": "Spark",
                            "plausible": True,
                            "position_index": 1,
                            "action_summary": "Used Spark for distributed batch processing",
                            "result_summary": "consistent ~18% latency gains",
                            "rationale": "The payments platform already processes data at scale, a typical Spark use case.",
                        },
                        {"keyword": "RAG", "plausible": False},
                    ]
                },
            ]
        )
        app.dependency_overrides[get_light_client] = lambda: jd_parse_fake
        app.dependency_overrides[get_heavy_client] = lambda: extend_fake
        try:
            r = client.get(f"{jd_url}/tailor", params={"k": 10})
            assert r.status_code == 200
            assert "Spark" in r.text
            assert "关键词：RAG" not in r.text  # 不合理的关联被过滤掉了，不应该出现在待确认列表里
        finally:
            app.dependency_overrides.pop(get_light_client, None)
            app.dependency_overrides.pop(get_heavy_client, None)

        hidden_match = re.search(r'name="hit_items_json" value=\'(.*?)\'', r.text, re.S)
        hit_items_json = html.unescape(hidden_match.group(1)) if hidden_match else "[]"

        confirm = client.post(
            f"{jd_url}/tailor/confirm",
            data={
                "k_value": "5",
                "hit_items_json": hit_items_json,
                "suggestion_count": "1",
                "accept_0": "on",
                "keyword_0": "Spark",
                "entry_0": str(position_id),
                "action_0": "Used Spark for distributed batch processing",
                "result_0": "consistent ~18% latency gains",
                "rationale_0": "Same batch ETL scale as the existing Hadoop pipeline",
            },
            follow_redirects=True,
        )
        assert confirm.status_code == 200
        assert "简历已生成" in confirm.text
        assert "Spark" in confirm.text


def test_tailor_confirm_rejects_unaccepted_suggestions():
    """没有勾选 accept 的建议，即使表单里带了它的数据，也不应该出现在最终简历里。"""
    with _client() as client:
        position_id = _seed_position_via_upload(client)
        jd_url = _seed_jd_with_score(client)

        confirm = client.post(
            f"{jd_url}/tailor/confirm",
            data={
                "k_value": "5",
                "hit_items_json": "[]",
                "suggestion_count": "1",
                # 注意：没有 accept_0 字段，模拟用户没有勾选这条建议
                "keyword_0": "Spark",
                "entry_0": str(position_id),
                "action_0": "Used Spark for distributed batch processing",
                "result_0": "consistent ~18% latency gains",
                "rationale_0": "r",
            },
            follow_redirects=True,
        )
        assert confirm.status_code == 200
        assert "Spark" not in confirm.text

        from app.core.db import get_sessionmaker
        from app.models.tables import ClaimedSkill

        db = get_sessionmaker()()
        try:
            assert db.query(ClaimedSkill).count() == 0
        finally:
            db.close()


def test_tailor_result_page_missing_resume_returns_404():
    with _client() as client:
        jd_url = _seed_jd_with_score(client)
        r = client.get(f"{jd_url}/resumes/99999")
        assert r.status_code == 404
