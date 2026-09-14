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


def test_job_mark_applied_manually_updates_status_and_hides_button():
    # Phase 4：手动兜底入口，覆盖插件没能自动识别到提交按钮、或者用户没
    # 安装插件的情况。
    with _client() as client:
        create = client.post(
            "/dashboard/jobs",
            data={
                "company": "Acme",
                "title": "Backend Engineer",
                "description_raw": "JD body text.",
                "source_url": "https://jobs.lever.co/acme/abc-123",
            },
            follow_redirects=False,
        )
        detail_url = create.headers["location"].split("?")[0]

        before = client.get(detail_url)
        assert "手动标记为已投递" in before.text

        r = client.post(f"{detail_url}/mark-applied", follow_redirects=True)
        assert r.status_code == 200
        assert "已标记为已投递" in r.text
        assert "已投递" in r.text
        # 已经是 applied 状态之后，手动标记按钮不应该再显示（没有意义了）
        assert "手动标记为已投递" not in r.text


def test_job_mark_applied_404_when_jd_missing():
    with _client() as client:
        r = client.post("/dashboard/jobs/9999/mark-applied")
        assert r.status_code == 404


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


def test_usage_page_shows_empty_state_before_any_llm_call():
    with _client() as client:
        r = client.get("/dashboard/usage")
        assert r.status_code == 200
        assert "还没有调用记录" in r.text
        assert "还没有任何调用记录" in r.text


def test_usage_page_shows_aggregated_stats_and_recent_log():
    from app.core.db import get_sessionmaker
    from app.models.tables import LLMUsageLog, ModelSlot

    with _client() as client:
        # 先随便发一个请求，确保 TestClient 的 lifespan 已经跑完迁移
        # （llm_usage_log 这张表才存在），再直接写库模拟历史调用记录——
        # 不通过真实 LLM 调用，这里只关心 Dashboard 页面渲染逻辑本身。
        client.get("/dashboard/jobs")

        db = get_sessionmaker()()
        try:
            db.add(
                LLMUsageLog(
                    slot=ModelSlot.LIGHT,
                    model_name="gpt-test",
                    ok=True,
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                )
            )
            db.add(
                LLMUsageLog(
                    slot=ModelSlot.LIGHT,
                    model_name="gpt-test",
                    ok=False,
                    error_message="调用超时",
                )
            )
            db.commit()
        finally:
            db.close()

        r = client.get("/dashboard/usage")
        assert r.status_code == 200
        assert "gpt-test" in r.text
        assert "调用超时" in r.text
        # 轻量模型：2 次调用（1 成功 1 失败），累计 15 token
        assert "2" in r.text
        assert "15" in r.text


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
                },
                # 事实护栏校验：延伸建议批量校验的那一次调用（RAG 已经被判定
                # 不合理提前过滤掉了，这里只需要覆盖剩下的 Spark 一条）。
                {"results": [{"consistent": True}]},
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

        # 简历结果页应该带一个可下载的 PDF 链接，且真的能下载到合法的 PDF 文件。
        pdf_match = re.search(r'href="(/dashboard/jobs/\d+/resumes/\d+/pdf)"', confirm.text)
        assert pdf_match is not None
        pdf_resp = client.get(pdf_match.group(1))
        assert pdf_resp.status_code == 200
        assert pdf_resp.headers["content-type"] == "application/pdf"
        assert pdf_resp.content[:4] == b"%PDF"


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


def test_resume_pdf_download_missing_resume_returns_404():
    with _client() as client:
        jd_url = _seed_jd_with_score(client)
        r = client.get(f"{jd_url}/resumes/99999/pdf")
        assert r.status_code == 404


# ---------- Phase 5：简历风格自定义 ----------


def test_tailor_draft_page_includes_style_selector():
    with _client() as client:
        _seed_position_via_upload(client)
        create = client.post(
            "/dashboard/jobs",
            data={
                "company": "Beta Inc",
                "title": "Big Data Engineer",
                "description_raw": "We need Hadoop experience for our data platform.",
            },
            follow_redirects=False,
        )
        jd_url = create.headers["location"].split("?")[0]

        # k=0：不做技能延伸，只需要一次 JD 解析调用，不需要另外准备重量模型
        # 的延伸建议响应——这个测试只关心"风格下拉框有没有渲染出来"，
        # 没必要把 build_resume_draft 完整的建议链路也搭一遍。
        jd_parse_fake = FakeLLMClient(responses=[dict(FAKE_JD_EXTRACTION_RESPONSE)])
        app.dependency_overrides[get_light_client] = lambda: jd_parse_fake
        app.dependency_overrides[get_heavy_client] = lambda: jd_parse_fake
        try:
            r = client.get(f"{jd_url}/tailor", params={"k": 0})
        finally:
            app.dependency_overrides.pop(get_light_client, None)
            app.dependency_overrides.pop(get_heavy_client, None)

        assert r.status_code == 200
        assert '<select name="render_choice">' in r.text
        assert "默认（简洁单栏）" in r.text
        assert "紧凑（更小间距，适合内容较多）" in r.text


def test_tailor_confirm_respects_selected_style():
    with _client() as client:
        position_id = _seed_position_via_upload(client)
        jd_url = _seed_jd_with_score(client)

        confirm = client.post(
            f"{jd_url}/tailor/confirm",
            data={"k_value": "0", "hit_items_json": "[]", "suggestion_count": "0", "render_choice": "style:compact"},
            follow_redirects=True,
        )
        assert confirm.status_code == 200
        assert 'value="style:compact" selected' in confirm.text


def test_tailor_confirm_falls_back_to_default_style_on_bogus_value():
    """表单被篡改提交了一个不在 AVAILABLE_RESUME_STYLES 里的风格值时，
    应该悄悄回退到 default，而不是让整个"生成简历"操作报错。"""
    with _client() as client:
        _seed_position_via_upload(client)
        jd_url = _seed_jd_with_score(client)

        confirm = client.post(
            f"{jd_url}/tailor/confirm",
            data={
                "k_value": "0",
                "hit_items_json": "[]",
                "suggestion_count": "0",
                "render_choice": "style:no-such-style",
            },
            follow_redirects=True,
        )
        assert confirm.status_code == 200
        assert "简历已生成" in confirm.text
        assert 'value="style:default" selected' in confirm.text


def test_resume_regenerate_pdf_switches_style_and_flashes_success():
    with _client() as client:
        _seed_position_via_upload(client)
        jd_url = _seed_jd_with_score(client)
        confirm = client.post(
            f"{jd_url}/tailor/confirm",
            data={"k_value": "0", "hit_items_json": "[]", "suggestion_count": "0", "render_choice": "style:default"},
            follow_redirects=True,
        )
        result_url = confirm.url.path

        regenerate = client.post(
            f"{result_url}/regenerate-pdf", data={"render_choice": "style:compact"}, follow_redirects=True
        )
        assert regenerate.status_code == 200
        assert "已按新风格/模板重新生成" in regenerate.text
        assert 'value="style:compact" selected' in regenerate.text


def test_resume_regenerate_pdf_missing_version_returns_404():
    with _client() as client:
        jd_url = _seed_jd_with_score(client)
        resume_id = 99999
        r = client.post(f"{jd_url}/resumes/{resume_id}/regenerate-pdf", data={"render_choice": "style:compact"})
        assert r.status_code == 404


def test_resume_regenerate_pdf_unknown_style_shows_friendly_flash_error():
    with _client() as client:
        _seed_position_via_upload(client)
        jd_url = _seed_jd_with_score(client)
        confirm = client.post(
            f"{jd_url}/tailor/confirm",
            data={"k_value": "0", "hit_items_json": "[]", "suggestion_count": "0", "render_choice": "style:default"},
            follow_redirects=True,
        )
        result_url = confirm.url.path

        regenerate = client.post(
            f"{result_url}/regenerate-pdf", data={"render_choice": "style:no-such-style"}, follow_redirects=True
        )
        assert regenerate.status_code == 200
        assert "未知的简历风格" in regenerate.text


# ---------- Phase 2 补完：问答题库（qa_bank） ----------


def test_qa_bank_list_page_renders_empty_state():
    with _client() as client:
        r = client.get("/dashboard/qa-bank")
        assert r.status_code == 200
        assert "还没有任何记录" in r.text


def test_qa_bank_manual_add_and_list():
    with _client() as client:
        r = client.post(
            "/dashboard/qa-bank",
            data={"question_text": "请简单介绍一下你自己", "answer_text": "我是一名数据工程师"},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "已保存" in r.text

        r = client.get("/dashboard/qa-bank")
        assert "请简单介绍一下你自己" in r.text
        assert "我是一名数据工程师" in r.text


def test_qa_bank_manual_add_rejects_blank_answer():
    with _client() as client:
        r = client.post(
            "/dashboard/qa-bank",
            data={"question_text": "你的职业规划是什么？", "answer_text": "   "},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "不能是空的" in r.text


def test_qa_bank_delete_entry():
    with _client() as client:
        client.post(
            "/dashboard/qa-bank",
            data={"question_text": "问题一", "answer_text": "答案一"},
            follow_redirects=True,
        )
        from app.core.db import get_sessionmaker
        from app.models.tables import QABankEntry

        db = get_sessionmaker()()
        try:
            entry_id = db.query(QABankEntry).one().id
        finally:
            db.close()

        r = client.post(f"/dashboard/qa-bank/{entry_id}/delete", follow_redirects=True)
        assert r.status_code == 200
        assert "已删除" in r.text
        assert "问题一" not in r.text


def test_qa_bank_suggest_without_light_model_shows_friendly_error():
    with _client() as client:
        r = client.get("/dashboard/qa-bank/suggest", follow_redirects=True)
        assert r.status_code == 200
        assert "还没配置" in r.text


def test_qa_bank_suggest_full_round_trip():
    with _client() as client:
        fake_light = FakeLLMClient(responses=[{"questions": ["请简单介绍一下你自己", "你最大的优势是什么？"]}])
        app.dependency_overrides[get_light_client] = lambda: fake_light
        try:
            r = client.get("/dashboard/qa-bank/suggest")
            assert r.status_code == 200
            assert "请简单介绍一下你自己" in r.text
        finally:
            app.dependency_overrides.pop(get_light_client, None)

        r = client.post(
            "/dashboard/qa-bank/suggest",
            data={
                "question_count": "2",
                "question_0": "请简单介绍一下你自己",
                "answer_0": "我是一名数据工程师",
                "question_1": "你最大的优势是什么？",
                "answer_1": "",  # 留空跳过
            },
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "已保存 1 条回答" in r.text

        r = client.get("/dashboard/qa-bank")
        assert "请简单介绍一下你自己" in r.text


# ---------- Phase 2 补完：合并冲突确认（近似重复贡献句 / 职位字段冲突）端到端 ----------


def test_resume_upload_conflict_renders_confirmation_page_then_resolve_applies_choices():
    fake_resume_1 = {
        "basic": {},
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
                        "bullets": ["Built a Hadoop-based ETL pipeline processing logs"],
                    }
                ],
            }
        ],
    }
    # 第二次上传：同一家公司/同一段职位，但 start_date 和已有记录不一致（职位字段冲突），
    # 且有一条贡献句和已有的高度相似、只是措辞不同（贡献句冲突）。
    fake_resume_2 = {
        "basic": {},
        "companies": [
            {
                "company_name": "Acme Corp",
                "positions": [
                    {
                        "position_title": "Backend Engineer",
                        "project_name": "Payments",
                        "start_date": "2021-06",
                        "end_date": None,
                        "is_current": True,
                        "bullets": ["Built a Hadoop based ETL pipeline for processing logs"],
                    }
                ],
            }
        ],
    }

    with _client() as client:
        fake_light_1 = FakeLLMClient(responses=[dict(fake_resume_1)])
        app.dependency_overrides[get_light_client] = lambda: fake_light_1
        try:
            r = client.post(
                "/dashboard/profile/resume",
                files={"resume_file": ("resume1.txt", io.BytesIO(b"first resume"), "text/plain")},
                follow_redirects=True,
            )
            assert r.status_code == 200
        finally:
            app.dependency_overrides.pop(get_light_client, None)

        fake_light_2 = FakeLLMClient(responses=[dict(fake_resume_2)])
        app.dependency_overrides[get_light_client] = lambda: fake_light_2
        try:
            r = client.post(
                "/dashboard/profile/resume",
                files={"resume_file": ("resume2.txt", io.BytesIO(b"second resume"), "text/plain")},
                follow_redirects=True,
            )
        finally:
            app.dependency_overrides.pop(get_light_client, None)

        # 有冲突时不会重定向回画像页，而是直接渲染确认页
        assert r.status_code == 200
        assert "确认合并冲突" in r.text
        assert "Built a Hadoop-based ETL pipeline processing logs" in r.text
        assert "Built a Hadoop based ETL pipeline for processing logs" in r.text
        assert "2022-01" in r.text
        assert "2021-06" in r.text

        # 从页面里把两条冲突的隐藏字段抠出来，模拟用户勾选"用新句替换旧句"/"采用新解析出的值"再提交
        conflict_count_match = re.search(r'name="conflict_count" value="(\d+)"', r.text)
        assert conflict_count_match
        assert conflict_count_match.group(1) == "2"

        def _hidden(name: str) -> str:
            m = re.search(rf'name="{name}" value="([^"]*)"', r.text)
            assert m, name
            return html.unescape(m.group(1))

        kinds = {}
        for i in range(2):
            kinds[i] = _hidden(f"kind_{i}")

        form_data = {"conflict_count": "2"}
        for i, kind in kinds.items():
            form_data[f"kind_{i}"] = kind
            form_data[f"position_id_{i}"] = _hidden(f"position_id_{i}")
            if kind == "bullet":
                form_data[f"existing_bullet_id_{i}"] = _hidden(f"existing_bullet_id_{i}")
                form_data[f"new_text_{i}"] = _hidden(f"new_text_{i}")
                form_data[f"choice_{i}"] = "replace"
            else:
                form_data[f"field_{i}"] = _hidden(f"field_{i}")
                form_data[f"new_value_{i}"] = _hidden(f"new_value_{i}")
                form_data[f"choice_{i}"] = "use_new"

        r = client.post("/dashboard/profile/merge-conflicts/resolve", data=form_data, follow_redirects=True)
        assert r.status_code == 200
        assert "已处理 2 条冲突" in r.text
        assert "其中 2 条采用了新内容" in r.text

        # 冲突已经按用户选择应用：贡献句被替换成新的措辞，start_date 采用了新值
        page = client.get("/dashboard/profile")
        assert "Built a Hadoop based ETL pipeline for processing logs" in page.text
        assert "Built a Hadoop-based ETL pipeline processing logs" not in page.text
        assert "2021-06" in page.text


def test_resume_upload_conflict_keep_old_default_leaves_data_unchanged():
    fake_resume_1 = {
        "basic": {},
        "companies": [
            {
                "company_name": "Beta Inc",
                "positions": [
                    {
                        "position_title": "Data Engineer",
                        "project_name": None,
                        "start_date": "2020-01",
                        "end_date": "2021-01",
                        "is_current": False,
                        "bullets": ["Migrated a legacy ETL pipeline to Airflow"],
                    }
                ],
            }
        ],
    }
    fake_resume_2 = {
        "basic": {},
        "companies": [
            {
                "company_name": "Beta Inc",
                "positions": [
                    {
                        "position_title": "Data Engineer",
                        "project_name": None,
                        "start_date": "2019-06",
                        "end_date": "2021-01",
                        "is_current": False,
                        "bullets": ["Migrated a legacy ETL pipeline over to Airflow"],
                    }
                ],
            }
        ],
    }
    with _client() as client:
        for fake_resume in (fake_resume_1, fake_resume_2):
            fake_light = FakeLLMClient(responses=[dict(fake_resume)])
            app.dependency_overrides[get_light_client] = lambda fl=fake_light: fl
            try:
                r = client.post(
                    "/dashboard/profile/resume",
                    files={"resume_file": ("resume.txt", io.BytesIO(b"resume text"), "text/plain")},
                    follow_redirects=True,
                )
            finally:
                app.dependency_overrides.pop(get_light_client, None)

        assert "确认合并冲突" in r.text
        conflict_count_match = re.search(r'name="conflict_count" value="(\d+)"', r.text)
        count = int(conflict_count_match.group(1))

        # 什么都不选（表单里 radio 默认就是 keep_old），直接提交
        form_data = {"conflict_count": str(count)}
        for i in range(count):
            kind_match = re.search(rf'name="kind_{i}" value="([^"]*)"', r.text)
            form_data[f"kind_{i}"] = kind_match.group(1)
            pos_match = re.search(rf'name="position_id_{i}" value="([^"]*)"', r.text)
            form_data[f"position_id_{i}"] = pos_match.group(1)

        r = client.post("/dashboard/profile/merge-conflicts/resolve", data=form_data, follow_redirects=True)
        assert r.status_code == 200
        assert "已处理" in r.text
        assert "其中 0 条采用了新内容" in r.text

        page = client.get("/dashboard/profile")
        assert "Migrated a legacy ETL pipeline to Airflow" in page.text
        assert "2020-01" in page.text
        assert "2019-06" not in page.text


# ---------- Phase 2 补完：工作经历树的手动增删改 ----------


def test_add_company_then_add_position_then_add_bullet_round_trip():
    with _client() as client:
        r = client.post(
            "/dashboard/profile/companies",
            data={"company_name": "Manual Corp"},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "已新增公司" in r.text
        assert "Manual Corp" in r.text

        from app.core.db import get_sessionmaker
        from app.models.tables import ExperienceEntry, ExperienceLevel

        db = get_sessionmaker()()
        try:
            company = db.query(ExperienceEntry).filter(ExperienceEntry.company_name == "Manual Corp").one()
            company_id = company.id
        finally:
            db.close()

        r = client.post(
            f"/dashboard/profile/companies/{company_id}/positions",
            data={
                "position_title": "Founding Engineer",
                "project_name": "",
                "start_date": "2023-01",
                "end_date": "",
                "is_current": "1",
            },
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "已新增职位" in r.text
        assert "Founding Engineer" in r.text

        db = get_sessionmaker()()
        try:
            position = (
                db.query(ExperienceEntry)
                .filter(ExperienceEntry.level == ExperienceLevel.POSITION, ExperienceEntry.parent_id == company_id)
                .one()
            )
            position_id = position.id
        finally:
            db.close()

        r = client.post(
            f"/dashboard/profile/positions/{position_id}/bullets",
            data={"content": "Shipped the first version of the product"},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "已新增贡献句" in r.text
        assert "Shipped the first version of the product" in r.text


def test_update_position_fields_persists_changes():
    with _client() as client:
        position_id = _seed_position_via_upload(client)

        r = client.post(
            f"/dashboard/profile/positions/{position_id}/update",
            data={
                "position_title": "Senior Backend Engineer",
                "project_name": "Payments",
                "start_date": "2022-06",
                "end_date": "",
                "is_current": "1",
            },
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "已更新" in r.text
        assert "Senior Backend Engineer" in r.text
        assert "2022-06" in r.text


def test_update_position_unknown_id_returns_404():
    with _client() as client:
        r = client.post(
            "/dashboard/profile/positions/99999/update",
            data={"position_title": "X", "project_name": "", "start_date": "", "end_date": "", "is_current": ""},
        )
        assert r.status_code == 404


def test_update_and_delete_bullet_round_trip():
    with _client() as client:
        position_id = _seed_position_via_upload(client)
        page = client.get(f"/dashboard/profile/positions/{position_id}")
        bullet_id_match = re.search(r"/dashboard/profile/bullets/(\d+)/update", page.text)
        assert bullet_id_match
        bullet_id = int(bullet_id_match.group(1))

        r = client.post(
            f"/dashboard/profile/bullets/{bullet_id}/update",
            data={"content": "Rewrote the bullet content entirely", "position_id": position_id},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "已更新贡献句" in r.text
        assert "Rewrote the bullet content entirely" in r.text

        r = client.post(
            f"/dashboard/profile/bullets/{bullet_id}/delete",
            data={"position_id": position_id},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "已删除贡献句" in r.text
        assert "Rewrote the bullet content entirely" not in r.text


def test_delete_position_cascades_and_delete_company_cascades():
    with _client() as client:
        position_id = _seed_position_via_upload(client)

        from app.core.db import get_sessionmaker
        from app.models.tables import ExperienceEntry

        db = get_sessionmaker()()
        try:
            position = db.get(ExperienceEntry, position_id)
            company_id = position.parent_id
        finally:
            db.close()

        r = client.post(
            f"/dashboard/profile/positions/{position_id}/delete",
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "已删除该段职位经历" in r.text

        r = client.get(f"/dashboard/profile/positions/{position_id}")
        assert r.status_code == 404

        r = client.post(
            f"/dashboard/profile/companies/{company_id}/delete",
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "已删除公司及其下所有经历" in r.text
        assert "Acme Corp" not in r.text
        assert "你最大的优势是什么？" not in r.text  # 跳过的问题不应该被保存进题库


# ---------- 用户反馈：调用 LLM 时页面不能看起来卡住（轻量方案：即时前端提示） ----------
#
# 这里只验证"会调用 LLM 的表单/链接确实带上了 data-llm-loading 属性"这个
# 静态标记本身——真正的加载提示是纯前端 JS 行为（监听 submit/click 事件插入
# 一个带小圆圈动画的提示），TestClient 不跑浏览器 JS，没办法端到端验证这一步，
# 这一点和 base.html 里那段脚本的注释是一致的。


def test_analyze_form_has_llm_loading_hint():
    with _client() as client:
        create = client.post(
            "/dashboard/jobs",
            data={"company": "Gamma LLC", "title": "Data Scientist", "description_raw": "Need Python."},
            follow_redirects=False,
        )
        jd_url = create.headers["location"].split("?")[0]
        r = client.get(jd_url)
        assert 'data-llm-loading="AI 正在分析并打分，请稍候…"' in r.text


def test_reanalyze_and_tailor_forms_have_llm_loading_hint():
    with _client() as client:
        jd_url = _seed_jd_with_score(client)
        r = client.get(jd_url)
        assert 'data-llm-loading="AI 正在重新分析并打分，请稍候…"' in r.text
        assert 'data-llm-loading="AI 正在生成简历草稿，请稍候…"' in r.text


def test_qa_bank_suggest_link_has_llm_loading_hint():
    with _client() as client:
        r = client.get("/dashboard/qa-bank")
        assert 'data-llm-loading="AI 正在生成常见问题，请稍候…"' in r.text


def test_resume_upload_form_has_llm_loading_hint():
    with _client() as client:
        r = client.get("/dashboard/profile")
        assert 'data-llm-loading="AI 正在解析简历，请稍候…"' in r.text


def test_interview_start_form_has_llm_loading_hint():
    with _client() as client:
        position_id = _seed_position_via_upload(client)
        r = client.get(f"/dashboard/profile/positions/{position_id}")
        assert 'data-llm-loading="AI 正在生成追问问题，请稍候…"' in r.text


def test_interview_answer_form_has_llm_loading_hint():
    with _client() as client:
        position_id = _seed_position_via_upload(client)
        fake_light = FakeLLMClient(responses=[dict(FAKE_QUESTIONS_RESPONSE)])
        app.dependency_overrides[get_light_client] = lambda: fake_light
        try:
            r = client.get(f"/dashboard/profile/positions/{position_id}/interview")
        finally:
            app.dependency_overrides.pop(get_light_client, None)
        assert 'data-llm-loading="AI 正在整理项目背景描述，请稍候…"' in r.text


def test_tailor_regenerate_draft_form_has_llm_loading_hint():
    with _client() as client:
        jd_url = _seed_jd_with_score(client)
        # k=0：不做技能延伸，画像里又没有任何经历可命中，所以这次 /tailor 不
        # 会真的调用 light/heavy 任何一个 client，两个 Fake 只是用来满足路由
        # "两个槽位都必须配置"这条前置检查。
        app.dependency_overrides[get_light_client] = lambda: FakeLLMClient(responses=[])
        app.dependency_overrides[get_heavy_client] = lambda: FakeLLMClient(responses=[])
        try:
            r = client.get(f"{jd_url}/tailor", params={"k": 0}, follow_redirects=True)
        finally:
            app.dependency_overrides.pop(get_light_client, None)
            app.dependency_overrides.pop(get_heavy_client, None)
        assert r.status_code == 200
        assert 'data-llm-loading="AI 正在重新生成简历草稿，请稍候…"' in r.text


# ---------- 打磨阶段后新增：教育经历 / 独立项目 / MD 模板库路由 ----------


def test_profile_add_update_delete_education_entry():
    with _client() as client:
        add = client.post(
            "/dashboard/profile/education",
            data={"school": "MIT", "degree": "BSc", "location": "Cambridge", "start_date": "2016-09", "end_date": "2020-06"},
            follow_redirects=True,
        )
        assert add.status_code == 200
        assert "已新增教育经历" in add.text
        assert "MIT" in add.text

        match = re.search(r"/dashboard/profile/education/(\d+)/delete", add.text)
        assert match, "没有在画像页面找到教育经历的删除链接"
        entry_id = int(match.group(1))

        update = client.post(
            f"/dashboard/profile/education/{entry_id}/update",
            data={"school": "MIT", "degree": "MSc", "location": "Cambridge"},
            follow_redirects=True,
        )
        assert update.status_code == 200
        assert "已更新教育经历" in update.text
        assert "MSc" in update.text

        delete = client.post(f"/dashboard/profile/education/{entry_id}/delete", follow_redirects=True)
        assert delete.status_code == 200
        assert "已删除教育经历" in delete.text
        assert "MIT" not in delete.text


def test_profile_add_education_requires_school():
    with _client() as client:
        r = client.post("/dashboard/profile/education", data={"school": ""}, follow_redirects=True)
        assert r.status_code == 200
        assert 'class="flash error"' in r.text


def test_profile_update_education_missing_id_returns_404():
    with _client() as client:
        r = client.post("/dashboard/profile/education/9999/update", data={"school": "X"})
        assert r.status_code == 404


def test_profile_add_project_with_bullet_then_delete():
    with _client() as client:
        add = client.post(
            "/dashboard/profile/projects",
            data={"project_name": "Side Bot", "start_date": "2023"},
            follow_redirects=True,
        )
        assert add.status_code == 200
        assert "已新增独立项目" in add.text
        assert "Side Bot" in add.text

        match = re.search(r"/dashboard/profile/projects/(\d+)/delete", add.text)
        assert match, "没有在画像页面找到独立项目的删除链接"
        project_id = int(match.group(1))

        bullet_add = client.post(
            f"/dashboard/profile/projects/{project_id}/bullets",
            data={"content": "Built a thing"},
            follow_redirects=True,
        )
        assert bullet_add.status_code == 200
        assert "已新增项目贡献句" in bullet_add.text
        assert "Built a thing" in bullet_add.text

        bullet_match = re.search(r"/dashboard/profile/project-bullets/(\d+)/delete", bullet_add.text)
        assert bullet_match, "没有在画像页面找到项目贡献句的删除链接"
        bullet_id = int(bullet_match.group(1))

        bullet_delete = client.post(f"/dashboard/profile/project-bullets/{bullet_id}/delete", follow_redirects=True)
        assert bullet_delete.status_code == 200
        assert "Built a thing" not in bullet_delete.text

        project_delete = client.post(f"/dashboard/profile/projects/{project_id}/delete", follow_redirects=True)
        assert project_delete.status_code == 200
        assert "已删除独立项目" in project_delete.text
        assert "Side Bot" not in project_delete.text


def test_profile_update_project_missing_id_returns_404():
    with _client() as client:
        r = client.post("/dashboard/profile/projects/9999/update", data={"project_name": "X"})
        assert r.status_code == 404


def test_resume_templates_page_lists_seeded_default_template():
    with _client() as client:
        r = client.get("/dashboard/resume-templates")
        assert r.status_code == 200
        assert "默认模板" in r.text


def test_resume_template_create_update_set_default_delete_round_trip():
    with _client() as client:
        client.get("/dashboard/resume-templates")  # 确保种子默认模板存在，这样后面删掉新模板不会撞到"最后一个不能删"
        create = client.post(
            "/dashboard/resume-templates",
            data={"name": "我的模板", "content": "# {{ basic.full_name }}"},
            follow_redirects=True,
        )
        assert create.status_code == 200
        assert "已新增模板" in create.text
        assert "我的模板" in create.text

        match = re.search(r"/dashboard/resume-templates/(\d+)/delete", create.text)
        assert match, "没有在模板库页面找到新模板的删除链接"
        # 页面上可能同时有默认模板和新模板的删除链接，取最后一个（新建的那个
        # 通常渲染在后面，因为默认模板置顶展开）——用 name 附近的上下文更稳妥。
        template_ids = [int(m) for m in re.findall(r"/dashboard/resume-templates/(\d+)/delete", create.text)]
        new_template_id = template_ids[-1]

        update = client.post(
            f"/dashboard/resume-templates/{new_template_id}/update",
            data={"name": "改名后的模板", "content": "## {{ basic.full_name }}"},
            follow_redirects=True,
        )
        assert update.status_code == 200
        assert "模板已更新" in update.text
        assert "改名后的模板" in update.text

        set_default = client.post(f"/dashboard/resume-templates/{new_template_id}/set-default", follow_redirects=True)
        assert set_default.status_code == 200
        assert "已设为默认模板" in set_default.text

        delete = client.post(f"/dashboard/resume-templates/{new_template_id}/delete", follow_redirects=True)
        assert delete.status_code == 200
        assert "已删除模板" in delete.text
        assert "改名后的模板" not in delete.text


def test_resume_template_create_rejects_broken_jinja_syntax():
    with _client() as client:
        r = client.post(
            "/dashboard/resume-templates",
            data={"name": "坏模板", "content": "{% for x in %}"},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert 'class="flash error"' in r.text
        # 校验失败不应该创建出一条能列出来的模板记录。
        list_page = client.get("/dashboard/resume-templates")
        assert "坏模板" not in list_page.text


def test_resume_template_delete_last_one_shows_friendly_error():
    with _client() as client:
        page = client.get("/dashboard/resume-templates")
        match = re.search(r"/dashboard/resume-templates/(\d+)/delete", page.text)
        assert match, "至少应该有一个种子默认模板"
        template_id = int(match.group(1))
        r = client.post(f"/dashboard/resume-templates/{template_id}/delete", follow_redirects=True)
        assert r.status_code == 200
        assert "至少要保留一个" in r.text


def test_resume_template_update_missing_id_returns_404():
    with _client() as client:
        r = client.post("/dashboard/resume-templates/9999/update", data={"name": "x", "content": "y"})
        assert r.status_code == 404


def test_tailor_draft_page_lists_md_templates_in_optgroup():
    with _client() as client:
        client.get("/dashboard/resume-templates")  # 确保种子默认模板存在
        _seed_position_via_upload(client)
        create = client.post(
            "/dashboard/jobs",
            data={
                "company": "Beta Inc",
                "title": "Big Data Engineer",
                "description_raw": "We need Hadoop experience for our data platform.",
            },
            follow_redirects=False,
        )
        jd_url = create.headers["location"].split("?")[0]

        # 跟 test_tailor_draft_page_includes_style_selector 一样：k=0 只需要
        # 一次 JD 解析调用，直接给 /tailor 这次请求配好两个槽位的 Fake 即可，
        # 不需要先跑一遍 /analyze。
        jd_parse_fake = FakeLLMClient(responses=[dict(FAKE_JD_EXTRACTION_RESPONSE)])
        app.dependency_overrides[get_light_client] = lambda: jd_parse_fake
        app.dependency_overrides[get_heavy_client] = lambda: jd_parse_fake
        try:
            r = client.get(f"{jd_url}/tailor", params={"k": 0})
        finally:
            app.dependency_overrides.pop(get_light_client, None)
            app.dependency_overrides.pop(get_heavy_client, None)

        assert r.status_code == 200
        assert "MD 模板" in r.text
        assert "默认模板" in r.text


def test_tailor_confirm_with_md_template_renders_via_template_path():
    with _client() as client:
        _seed_position_via_upload(client)

        create_template = client.post(
            "/dashboard/resume-templates",
            data={"name": "我的自定义模板", "content": "# {{ basic.full_name }}\n\n自定义内容标记"},
            follow_redirects=True,
        )
        template_ids = [int(m) for m in re.findall(r"/dashboard/resume-templates/(\d+)/delete", create_template.text)]
        template_id = template_ids[-1]

        jd_url = _seed_jd_with_score(client)
        confirm = client.post(
            f"{jd_url}/tailor/confirm",
            data={
                "k_value": "0",
                "hit_items_json": "[]",
                "suggestion_count": "0",
                "render_choice": f"template:{template_id}",
            },
            follow_redirects=True,
        )
        assert confirm.status_code == 200
        assert "简历已生成" in confirm.text
        assert "自定义内容标记" in confirm.text


def test_tailor_confirm_with_unknown_template_id_shows_friendly_flash_error():
    with _client() as client:
        _seed_position_via_upload(client)
        jd_url = _seed_jd_with_score(client)
        confirm = client.post(
            f"{jd_url}/tailor/confirm",
            data={
                "k_value": "0",
                "hit_items_json": "[]",
                "suggestion_count": "0",
                "render_choice": "template:9999",
            },
            follow_redirects=True,
        )
        assert confirm.status_code == 200
        assert "不存在了" in confirm.text
