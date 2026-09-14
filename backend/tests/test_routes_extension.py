"""Phase 3：插件专用 REST 接口 `/api/jobs`。

插件在职位详情页解析出 JD 之后一键发过来入库，这里复用 test_handshake.py 里已经
验证过的配对鉴权套路（Origin 必须是 chrome-extension:// 开头 + 正确的 pairing
token），重点覆盖：鉴权拦截、正常入库、空描述被拒绝、返回的 dashboard_url 能在
Dashboard 里查到对应记录。
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps_llm import get_light_client
from app.core.db import get_sessionmaker
from app.core.llm_client import FakeLLMClient
from app.main import app
from app.models.tables import JDRecord, JDStatus, QABankEntry, ResumeVersion

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


# ---------- Phase 4：自动化填表 ----------


def test_autofill_plan_rejects_missing_auth():
    with TestClient(app) as client:
        r = client.post("/api/autofill/plan", json={"fields": []})
        assert r.status_code == 403


def test_autofill_plan_keyword_match_without_llm_configured():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        client.post("/dashboard/profile", data={"email": "a@example.com"})
        r = client.post(
            "/api/autofill/plan",
            json={"fields": [{"field_id": "f1", "label": "Email Address", "input_type": "email"}]},
            headers=headers,
        )
        assert r.status_code == 200
        plan = r.json()["plan"]
        assert plan[0]["action"] == "fill"
        assert plan[0]["value"] == "a@example.com"


def test_autofill_plan_compliance_choice_field_always_skipped_even_with_llm_configured():
    # 关键安全回归：哪怕轻量模型已配置、且模型把这个标签映射成了
    # work_authorization,选择类控件也必须跳过——不能因为换成走 LLM 分支就
    # 绕过了合规安全限制。
    fake = FakeLLMClient(
        responses=[{"mappings": [{"index": 0, "field_key": "work_authorization", "is_essay_question": False}]}]
    )
    app.dependency_overrides[get_light_client] = lambda: fake
    try:
        with TestClient(app) as client:
            headers = _auth_headers(client)
            client.post("/dashboard/profile", data={"work_authorization": "US Citizen"})
            r = client.post(
                "/api/autofill/plan",
                json={
                    "fields": [
                        {
                            "field_id": "f1",
                            "label": "Do you require visa sponsorship?",
                            "input_type": "radio",
                            "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}],
                        }
                    ]
                },
                headers=headers,
            )
            assert r.status_code == 200
            plan = r.json()["plan"]
            assert plan[0]["action"] == "skip"
    finally:
        app.dependency_overrides.pop(get_light_client, None)


def test_autofill_qa_save_persists_entry():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        r = client.post(
            "/api/autofill/qa-save",
            json={"question_text": "为什么想加入我们公司", "answer_text": "因为贵公司的技术栈很吸引我"},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["saved"] is True

        db = get_sessionmaker()()
        try:
            entry = db.query(QABankEntry).filter(QABankEntry.question_text == "为什么想加入我们公司").first()
            assert entry is not None
            assert entry.answer_text == "因为贵公司的技术栈很吸引我"
            assert entry.embedding is not None
        finally:
            db.close()


def test_autofill_qa_save_blank_answer_not_saved():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        r = client.post(
            "/api/autofill/qa-save",
            json={"question_text": "一道问题", "answer_text": "   "},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["saved"] is False


def test_autofill_resume_pdf_404_when_jd_missing():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        r = client.get("/api/autofill/resume-pdf/9999", headers=headers)
        assert r.status_code == 404


def test_autofill_resume_pdf_404_when_no_resume_generated_yet():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        create = client.post(
            "/dashboard/jobs",
            data={"company": "Acme", "title": "Eng", "description_raw": "JD body text."},
            follow_redirects=False,
        )
        jd_id = create.headers["location"].split("?")[0].rstrip("/").split("/")[-1]
        r = client.get(f"/api/autofill/resume-pdf/{jd_id}", headers=headers)
        assert r.status_code == 404


def test_autofill_resume_pdf_returns_latest_version():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        create = client.post(
            "/dashboard/jobs",
            data={"company": "Acme", "title": "Eng", "description_raw": "JD body text."},
            follow_redirects=False,
        )
        jd_id = int(create.headers["location"].split("?")[0].rstrip("/").split("/")[-1])

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "resume.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 fake pdf content")

            db = get_sessionmaker()()
            try:
                older = ResumeVersion(jd_id=jd_id, k_value=3, style_id="default", pdf_path=str(pdf_path))
                db.add(older)
                db.commit()
                newer = ResumeVersion(jd_id=jd_id, k_value=5, style_id="default", pdf_path=str(pdf_path))
                db.add(newer)
                db.commit()
            finally:
                db.close()

            r = client.get(f"/api/autofill/resume-pdf/{jd_id}", headers=headers)
            assert r.status_code == 200
            assert r.headers["content-type"] == "application/pdf"


def test_mark_job_applied_updates_status():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        create = client.post(
            "/dashboard/jobs",
            data={"company": "Acme", "title": "Eng", "description_raw": "JD body text."},
            follow_redirects=False,
        )
        jd_id = int(create.headers["location"].split("?")[0].rstrip("/").split("/")[-1])

        r = client.post(f"/api/jobs/{jd_id}/mark-applied", headers=headers)
        assert r.status_code == 200
        assert r.json()["status"] == "applied"

        db = get_sessionmaker()()
        try:
            jd = db.get(JDRecord, jd_id)
            assert jd.status == JDStatus.APPLIED
        finally:
            db.close()


def test_mark_job_applied_404_when_jd_missing():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        r = client.post("/api/jobs/9999/mark-applied", headers=headers)
        assert r.status_code == 404


# ---------- LinkedIn 画像导入（打磨阶段后新增） ----------


def test_submit_linkedin_profile_rejects_missing_auth():
    with TestClient(app) as client:
        r = client.post("/api/linkedin-profile", json={"skills": ["Python"]})
        assert r.status_code == 403


def test_submit_linkedin_profile_rejects_empty_payload():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        r = client.post("/api/linkedin-profile", json={}, headers=headers)
        assert r.status_code == 422


def test_submit_linkedin_profile_merges_into_profile_and_returns_dashboard_url():
    from app.core.db import get_sessionmaker
    from app.models.tables import EducationEntry, ExperienceEntry, ExperienceLevel, PersonalProject, ProfileSkill

    with TestClient(app) as client:
        headers = _auth_headers(client)
        r = client.post(
            "/api/linkedin-profile",
            json={
                "basic": {
                    "full_name": "Alice Example",
                    "target_title": "Senior Backend Engineer",
                    "current_location": "Toronto, ON",
                    "linkedin_url": "https://www.linkedin.com/in/alice-example",
                    "resume_summary": "Backend engineer with 8 years of experience.",
                },
                "skills": ["Python", "PostgreSQL", "Python"],
                "education_entries": [
                    {"school": "University of Waterloo", "degree": "BASc Computer Engineering", "start_date": "2014", "end_date": "2018"}
                ],
                "projects": [
                    {
                        "project_name": "JobPilot",
                        "start_date": "2025-01",
                        "is_current": True,
                        "company_tag": "Acme Corp",
                        "bullets": ["Built an AI-assisted job application tool"],
                    }
                ],
                "companies": [
                    {
                        "company_name": "Acme Corp",
                        "positions": [
                            {
                                "position_title": "Backend Engineer",
                                "start_date": "2022-01",
                                "is_current": True,
                                "bullets": ["Built a payments service"],
                            }
                        ],
                    }
                ],
                "source_url": "https://www.linkedin.com/in/alice-example",
            },
            headers=headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["dashboard_url"].endswith("/dashboard/profile")
        assert body["companies_added"] == 1
        assert body["positions_added"] == 1
        assert body["bullets_added"] == 1
        assert body["education_added"] == 1
        assert body["projects_added"] == 1
        assert body["project_bullets_added"] == 1
        # "Python" 传了两次，去重之后只应该新增两条
        assert body["skills_added"] == 2
        assert "full_name" in body["basic_fields_filled"]
        assert body["has_conflicts"] is False

        db = get_sessionmaker()()
        try:
            skills = {s.skill_name for s in db.query(ProfileSkill).all()}
            assert skills == {"Python", "PostgreSQL"}

            education = db.query(EducationEntry).all()
            assert len(education) == 1
            assert education[0].school == "University of Waterloo"

            project = db.query(PersonalProject).filter(PersonalProject.project_name == "JobPilot").one()
            assert project.company_tag == "Acme Corp"

            company = (
                db.query(ExperienceEntry)
                .filter(ExperienceEntry.level == ExperienceLevel.COMPANY, ExperienceEntry.company_name == "Acme Corp")
                .one()
            )
            assert len(company.children) == 1
        finally:
            db.close()

        profile_page = client.get("/dashboard/profile")
        assert "Alice Example" in profile_page.text
        assert "Python" in profile_page.text
        assert "JobPilot" in profile_page.text


def test_submit_linkedin_profile_second_import_only_fills_blank_and_dedupes():
    with TestClient(app) as client:
        headers = _auth_headers(client)
        first_payload = {
            "basic": {"full_name": "Alice Example"},
            "skills": ["Python"],
            "companies": [
                {
                    "company_name": "Acme Corp",
                    "positions": [{"position_title": "Backend Engineer", "bullets": ["Built a payments service"]}],
                }
            ],
        }
        r1 = client.post("/api/linkedin-profile", json=first_payload, headers=headers)
        assert r1.status_code == 200

        # 第二次导入：姓名已经有值了（不会被覆盖），同一个技能名再传一次不
        # 应该重复新增，同一条职位下的同一句贡献句也不应该重复新增。
        second_payload = {
            "basic": {"full_name": "Someone Else", "target_title": "Staff Engineer"},
            "skills": ["python"],  # 大小写不同，但本质是同一个技能
            "companies": [
                {
                    "company_name": "acme corp",  # 大小写不同，应该匹配到同一家公司
                    "positions": [{"position_title": "Backend Engineer", "bullets": ["Built a payments service"]}],
                }
            ],
        }
        r2 = client.post("/api/linkedin-profile", json=second_payload, headers=headers)
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["companies_added"] == 0
        assert body2["bullets_added"] == 0
        assert body2["skills_added"] == 0
        assert "target_title" in body2["basic_fields_filled"]
        assert "full_name" not in body2["basic_fields_filled"]

        profile_page = client.get("/dashboard/profile")
        assert "Alice Example" in profile_page.text
        assert "Someone Else" not in profile_page.text
