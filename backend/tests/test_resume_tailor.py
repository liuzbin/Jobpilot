"""Phase 2：简历重制（app/services/resume_tailor.py）。

覆盖 K 值驱动的关键词命中/延伸判定（含确定性要求）、延伸建议生成时对
"不合理关联"的过滤、确认落地后 ClaimedSkill 的持久化/复用，以及最关键的一条
回归测试：ClaimedSkill 的存在不能影响 Phase 1 打分引擎的客观分数——这是
"核心价值主张"（诚实的差距诊断）成立的前提。
"""

from __future__ import annotations

import pytest

from app.core.llm_client import FakeLLMClient
from app.models.tables import ClaimedSkill, ExperienceEntry, ExperienceLevel
from app.services.analysis import analyze_jd
from app.services.jd_ingest import create_jd
from app.services.profile_deepening import merge_background_answers
from app.services.profile_service import merge_parsed_experience, update_profile_basic
from app.services.resume_tailor import (
    JDNotFoundError,
    _batch_validate_facts,
    build_resume_draft,
    collect_jd_keywords,
    confirm_and_finalize,
    find_hit_bullets,
    find_missing_keywords,
    generate_extension_suggestions,
    rewrite_hit_bullets,
    select_keywords_to_extend,
    validate_item_facts,
)


def _seed_experience(db_session) -> int:
    parsed = {
        "basic": {},
        "companies": [
            {
                "company_name": "Acme Corp",
                "positions": [
                    {
                        "position_title": "Data Engineer",
                        "project_name": "Big Data Platform",
                        "start_date": "2022-01",
                        "end_date": None,
                        "is_current": True,
                        "bullets": ["Built a Hadoop-based ETL pipeline processing 2TB of logs daily"],
                    }
                ],
            }
        ],
    }
    merge_parsed_experience(db_session, parsed)
    position = (
        db_session.query(ExperienceEntry)
        .filter(ExperienceEntry.level == ExperienceLevel.POSITION)
        .one()
    )
    position.background_notes = "该平台每日处理2TB日志数据，基于Hadoop生态做批处理ETL。"
    bullet = position.bullets[0]
    bullet.keywords = ["Hadoop", "ETL"]
    bullet.action_summary = "Built ETL pipeline"
    bullet.result_summary = "Reduced latency by 18%"
    db_session.commit()
    return position.id


# ---------- 纯函数：关键词命中/缺失判定 ----------


def test_collect_jd_keywords_dedupes_and_prioritizes_key_skills():
    jd_parsed = {"key_skills": ["Hadoop", "hadoop ", "Spark"], "plus_skills": ["Spark", "Kafka"]}
    assert collect_jd_keywords(jd_parsed) == ["Hadoop", "Spark", "Kafka"]


def test_collect_jd_keywords_handles_missing_fields():
    assert collect_jd_keywords({}) == []


def test_find_missing_keywords_is_case_insensitive(db_session):
    position_id = _seed_experience(db_session)
    position = db_session.get(ExperienceEntry, position_id)
    missing = find_missing_keywords(["hadoop", "Spark", "RAG"], [position])
    assert missing == ["Spark", "RAG"]  # hadoop 大小写不同也算命中，被排除


def test_find_hit_bullets_reports_matched_keywords(db_session):
    position_id = _seed_experience(db_session)
    position = db_session.get(ExperienceEntry, position_id)
    hits = find_hit_bullets(["Hadoop", "Spark"], [position])
    assert len(hits) == 1
    assert hits[0]["matched_keywords"] == ["Hadoop"]
    assert hits[0]["company_name"] == "Acme Corp"


def test_select_keywords_to_extend_k0_returns_empty():
    assert select_keywords_to_extend(["Spark", "RAG", "Kafka"], 0) == []


def test_select_keywords_to_extend_k10_returns_all():
    missing = ["Spark", "RAG", "Kafka"]
    assert select_keywords_to_extend(missing, 10) == missing


def test_select_keywords_to_extend_scales_proportionally_with_ceiling():
    missing = ["A", "B", "C", "D"]
    # ceil(4 * 3/10) = ceil(1.2) = 2
    assert select_keywords_to_extend(missing, 3) == ["A", "B"]
    # ceil(4 * 5/10) = 2
    assert select_keywords_to_extend(missing, 5) == ["A", "B"]
    # ceil(4 * 8/10) = ceil(3.2) = 4
    assert select_keywords_to_extend(missing, 8) == missing


def test_select_keywords_to_extend_is_deterministic():
    missing = ["A", "B", "C", "D", "E"]
    results = [select_keywords_to_extend(missing, 6) for _ in range(5)]
    assert all(r == results[0] for r in results)


def test_select_keywords_to_extend_empty_missing_list():
    assert select_keywords_to_extend([], 10) == []


# ---------- 命中 bullet 的措辞重组 ----------


def test_rewrite_hit_bullets_empty_input_skips_llm_call():
    fake = FakeLLMClient(responses=[])
    assert rewrite_hit_bullets([], jd=None, llm_client=fake) == []
    assert fake.calls == []


def test_rewrite_hit_bullets_falls_back_to_original_when_model_omits_fields(db_session):
    position_id = _seed_experience(db_session)
    position = db_session.get(ExperienceEntry, position_id)
    hits = find_hit_bullets(["Hadoop"], [position])
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Hadoop experience.")

    fake = FakeLLMClient(responses=[{"rewritten": [{}]}])  # 模型没给出新措辞
    rewritten = rewrite_hit_bullets(hits, jd, fake)
    assert rewritten[0]["action_summary"] == hits[0]["action_summary"]
    assert rewritten[0]["result_summary"] == hits[0]["result_summary"]


# ---------- 事实字段护栏校验 ----------


def test_validate_item_facts_rule_layer_catches_injected_fake_company():
    """自测方法里明确要求的场景：故意在生成文本里注入一个不存在的公司名，
    断言护栏能拦截——规则层本身就是纯字符串扫描，不依赖模型这次判断得准不准，
    所以这里故意让模型层返回 consistent=True（模拟模型没识别出问题），验证
    规则层单独也必须能拦下来。"""
    fake = FakeLLMClient(responses=[{"results": [{"consistent": True}]}])
    item = {
        "action_summary": "曾在字节跳动科技有限公司主导搭建这条数据管道",
        "result_summary": "提升处理效率 20%",
    }
    violations = validate_item_facts(item, "Acme Corp", {"Acme Corp"}, fake)
    assert violations
    assert any("字节跳动" in v for v in violations)


def test_validate_item_facts_rule_layer_does_not_flag_real_company():
    fake = FakeLLMClient(responses=[{"results": [{"consistent": True}]}])
    item = {"action_summary": "在 Acme Corp 期间负责搭建实时数据管道", "result_summary": None}
    assert validate_item_facts(item, "Acme Corp", {"Acme Corp"}, fake) == []


def test_validate_item_facts_model_layer_catches_issue_rule_misses():
    """规则层只认公司名后缀，抓不住"提到不存在的学历/时间段"这类问题，
    这条测试专门验证轻量模型层能补上这一块。"""
    fake = FakeLLMClient(
        responses=[{"results": [{"consistent": False, "issues": ["提到了博士学位，画像里没有这条学历记录"]}]}]
    )
    item = {"action_summary": "作为博士期间的研究项目负责人主导了这项工作", "result_summary": None}
    assert validate_item_facts(item, "Acme Corp", {"Acme Corp"}, fake) == ["提到了博士学位，画像里没有这条学历记录"]


def test_validate_item_facts_passes_when_both_layers_consistent():
    fake = FakeLLMClient(responses=[{"results": [{"consistent": True}]}])
    item = {"action_summary": "负责搭建实时数据管道", "result_summary": "提升处理效率 20%"}
    assert validate_item_facts(item, "Acme Corp", {"Acme Corp"}, fake) == []


def test_validate_item_facts_skips_llm_call_for_blank_text():
    fake = FakeLLMClient(responses=[])
    assert validate_item_facts({"action_summary": "", "result_summary": None}, "Acme Corp", {"Acme Corp"}, fake) == []
    assert fake.calls == []


def test_batch_validate_facts_defensively_treats_missing_model_result_as_consistent():
    """模型批量返回的 results 数量少于输入条目数量时（防御性场景，仿照
    extract_bullet_triads 的补空策略），缺失的那条按"规则层说了算"处理，
    不额外报错、也不误伤规则层本来就判定通过的条目。"""
    fake = FakeLLMClient(responses=[{"results": [{"consistent": True}]}])  # 只返回 1 条，但传了 2 个 item
    items = [
        {"action_summary": "在 Acme Corp 做了这件事", "result_summary": None},
        {"action_summary": "又做了另一件真实的事", "result_summary": None},
    ]
    result = _batch_validate_facts(items, ["Acme Corp", "Acme Corp"], {"Acme Corp"}, fake)
    assert result == [[], []]


def test_build_resume_draft_guardrail_blocks_fabricated_company_name_after_failed_retry(db_session):
    """端到端场景：重写步骤的模型输出里混进了一个虚构公司名，轻量模型这次也
    没识别出来（consistent 模拟成 True），但规则层必须单独拦下；重试一次
    模型依然编造的话，最终必须回退到真实原文，绝不能把虚构公司名带出
    build_resume_draft。"""
    position_id = _seed_experience(db_session)
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Hadoop experience.")

    jd_parse_fake = FakeLLMClient(
        responses=[
            {"required_years": None, "required_education": None, "required_clearance": False,
             "plus_skills": [], "core_responsibilities": [], "key_skills": ["Hadoop"]},
            {"results": [{"consistent": True}]},  # 命中项批量校验：模型没识别出问题
            {"results": [{"consistent": True}]},  # 重试后单条复核：模型还是没识别出问题
        ]
    )
    fabricated = {
        "rewritten": [
            {
                "action_summary": "曾在字节跳动科技有限公司主导搭建这条 Hadoop 流水线",
                "result_summary": "Reduced latency by 18%",
            }
        ]
    }
    rewrite_fake = FakeLLMClient(responses=[fabricated, fabricated])  # 重试后仍然编造

    draft = build_resume_draft(db_session, jd.id, 0, jd_parse_fake, rewrite_fake)

    assert len(draft.hit_items) == 1
    assert "字节跳动" not in draft.hit_items[0]["action_summary"]
    assert draft.hit_items[0]["action_summary"] == "Built ETL pipeline"  # 回退到真实原文


def test_build_resume_draft_guardrail_accepts_clean_content_after_successful_retry(db_session):
    """第一次生成的内容被轻量模型判定不一致，重试后模型给出干净的措辞，
    这次校验通过——验证"重试成功就采用重试结果"这条路径，而不是一律回退。"""
    position_id = _seed_experience(db_session)
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Hadoop experience.")

    jd_parse_fake = FakeLLMClient(
        responses=[
            {"required_years": None, "required_education": None, "required_clearance": False,
             "plus_skills": [], "core_responsibilities": [], "key_skills": ["Hadoop"]},
            {"results": [{"consistent": False, "issues": ["提到了不存在的公司"]}]},  # 第一次：模型发现问题
            {"results": [{"consistent": True}]},  # 重试后单条复核：干净了
        ]
    )
    fabricated = {"rewritten": [{"action_summary": "曾在字节跳动负责这条流水线", "result_summary": "Reduced latency by 18%"}]}
    clean = {"rewritten": [{"action_summary": "Led the Hadoop ETL pipeline rollout", "result_summary": "Reduced latency by 18%"}]}
    rewrite_fake = FakeLLMClient(responses=[fabricated, clean])

    draft = build_resume_draft(db_session, jd.id, 0, jd_parse_fake, rewrite_fake)
    assert draft.hit_items[0]["action_summary"] == "Led the Hadoop ETL pipeline rollout"


def test_build_resume_draft_drops_extension_suggestion_that_fails_guardrail_twice(db_session):
    """延伸建议这边没有"真实原文"可以回退，两次都没通过校验就必须整条丢弃，
    不能出现在待确认列表里交给用户。"""
    position_id = _seed_experience(db_session)
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Spark experience.")

    jd_parse_fake = FakeLLMClient(
        responses=[
            {"required_years": None, "required_education": None, "required_clearance": False,
             "plus_skills": [], "core_responsibilities": [], "key_skills": ["Spark"]},
            {"results": [{"consistent": True}]},  # 延伸建议批量校验：规则层单独拦下
            {"results": [{"consistent": True}]},  # 重试后单条复核：规则层还是拦下
        ]
    )
    fabricated_suggestion = {
        "suggestions": [
            {
                "keyword": "Spark",
                "plausible": True,
                "position_index": 1,
                "action_summary": "在字节跳动科技有限公司使用 Spark 处理批量数据",
                "result_summary": "consistent ~18% latency gains",
                "rationale": "同等规模的批处理场景",
            }
        ]
    }
    heavy_fake = FakeLLMClient(responses=[fabricated_suggestion, fabricated_suggestion])

    draft = build_resume_draft(db_session, jd.id, 10, jd_parse_fake, heavy_fake)
    assert draft.suggestions == []


# ---------- 延伸建议生成：过滤不合理关联 ----------


def test_generate_extension_suggestions_filters_implausible_and_maps_position(db_session):
    position_id = _seed_experience(db_session)
    position = db_session.get(ExperienceEntry, position_id)
    fake = FakeLLMClient(
        responses=[
            {
                "suggestions": [
                    {
                        "keyword": "Spark",
                        "plausible": True,
                        "position_index": 1,
                        "action_summary": "Used Spark for batch processing",
                        "result_summary": "in line with ~18% latency gains elsewhere",
                        "rationale": "Same ETL scale as the Hadoop pipeline",
                    },
                    {"keyword": "RAG", "plausible": False},
                ]
            }
        ]
    )
    suggestions = generate_extension_suggestions([position], ["Spark", "RAG"], fake)
    assert len(suggestions) == 1
    assert suggestions[0]["keyword"] == "Spark"
    assert suggestions[0]["experience_entry_id"] == position.id


def test_generate_extension_suggestions_empty_keywords_skips_llm_call():
    fake = FakeLLMClient(responses=[])
    assert generate_extension_suggestions([], [], fake) == []
    assert fake.calls == []


def test_generate_extension_suggestions_ignores_out_of_range_position_index(db_session):
    position_id = _seed_experience(db_session)
    position = db_session.get(ExperienceEntry, position_id)
    fake = FakeLLMClient(
        responses=[
            {
                "suggestions": [
                    {"keyword": "Spark", "plausible": True, "position_index": 99, "action_summary": "x"}
                ]
            }
        ]
    )
    assert generate_extension_suggestions([position], ["Spark"], fake) == []


# ---------- 端到端草稿 + 确认落地 ----------


def test_build_resume_draft_not_found_jd(db_session):
    fake = FakeLLMClient(responses=[])
    with pytest.raises(JDNotFoundError):
        build_resume_draft(db_session, 9999, 5, fake, fake)


def test_build_resume_draft_k0_produces_no_suggestions(db_session):
    position_id = _seed_experience(db_session)
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Spark experience.")
    jd_parse_fake = FakeLLMClient(
        responses=[
            {"required_years": None, "required_education": None, "required_clearance": False,
             "plus_skills": [], "core_responsibilities": [], "key_skills": ["Hadoop", "Spark"]},
            {"results": [{"consistent": True}]},  # 事实护栏校验：命中项批量校验的那一次调用
        ]
    )
    rewrite_fake = FakeLLMClient(responses=[{"rewritten": [{"action_summary": "x", "result_summary": "y"}]}])
    draft = build_resume_draft(db_session, jd.id, 0, jd_parse_fake, rewrite_fake)
    assert draft.suggestions == []
    assert len(draft.hit_items) == 1


def test_confirm_and_finalize_persists_claimed_skill_and_resume_version(db_session):
    position_id = _seed_experience(db_session)
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Spark experience.")

    hit_items = find_hit_bullets(["Hadoop"], [db_session.get(ExperienceEntry, position_id)])
    accepted = [
        {
            "keyword": "Spark",
            "experience_entry_id": position_id,
            "action_summary": "Used Spark for batch processing",
            "result_summary": "consistent ~18% latency gains",
            "rationale": "Same ETL scale as the Hadoop pipeline",
        }
    ]
    resume_version = confirm_and_finalize(db_session, jd.id, 5, hit_items, accepted)

    assert resume_version.id is not None
    assert resume_version.k_value == 5
    assert "Spark" in resume_version.markdown_text
    assert resume_version.pdf_path is not None
    from pathlib import Path

    pdf_file = Path(resume_version.pdf_path)
    assert pdf_file.exists()
    assert pdf_file.read_bytes()[:4] == b"%PDF"

    claimed = db_session.query(ClaimedSkill).all()
    assert len(claimed) == 1
    assert claimed[0].skill_name == "Spark"
    assert claimed[0].experience_entry_id == position_id


def test_confirm_and_finalize_upserts_claimed_skill_on_repeat(db_session):
    position_id = _seed_experience(db_session)
    jd1 = create_jd(db_session, company="Beta", title="Eng1", description_raw="Need Spark.")
    jd2 = create_jd(db_session, company="Gamma", title="Eng2", description_raw="Need Spark too.")

    suggestion_v1 = {
        "keyword": "Spark", "experience_entry_id": position_id,
        "action_summary": "v1 action", "result_summary": "v1 result", "rationale": "v1 rationale",
    }
    confirm_and_finalize(db_session, jd1.id, 5, [], [suggestion_v1])

    suggestion_v2 = {
        "keyword": "Spark", "experience_entry_id": position_id,
        "action_summary": "v2 action (updated wording)", "result_summary": "v2 result", "rationale": "v2 rationale",
    }
    confirm_and_finalize(db_session, jd2.id, 5, [], [suggestion_v2])

    claimed = db_session.query(ClaimedSkill).filter(ClaimedSkill.skill_name == "Spark").all()
    assert len(claimed) == 1  # 更新而不是重复插入
    assert claimed[0].action_summary == "v2 action (updated wording)"


def test_confirm_and_finalize_missing_jd_raises(db_session):
    with pytest.raises(JDNotFoundError):
        confirm_and_finalize(db_session, 9999, 5, [], [])


def test_confirm_and_finalize_degrades_gracefully_when_pdf_rendering_unavailable(db_session, monkeypatch):
    """回归测试：WeasyPrint 没装好系统依赖时（Windows 上常见的
    'libgobject-2.0-0' 加载失败），`confirm_and_finalize` 不应该跟着报错——
    resume_json/markdown_text 才是事实来源，PDF 只是附加产物，缺了它应该
    只是 `pdf_path` 为空，简历确认这个操作本身必须成功。"""
    from app.services import resume_pdf

    monkeypatch.setattr(resume_pdf, "_WeasyPrintHTML", None)
    monkeypatch.setattr(resume_pdf, "_WEASYPRINT_IMPORT_ERROR", ImportError("simulated missing libgobject"))

    position_id = _seed_experience(db_session)
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Spark experience.")
    hit_items = find_hit_bullets(["Hadoop"], [db_session.get(ExperienceEntry, position_id)])

    resume_version = confirm_and_finalize(db_session, jd.id, 5, hit_items, [])

    assert resume_version.id is not None
    assert resume_version.markdown_text  # 事实来源不受影响
    assert resume_version.pdf_path is None


# ---------- Phase 5：换个风格重新渲染 PDF，不重新走 LLM ----------


def test_regenerate_resume_pdf_switches_style_without_touching_facts(db_session):
    from pathlib import Path

    from app.services.resume_tailor import regenerate_resume_pdf

    position_id = _seed_experience(db_session)
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Spark experience.")
    hit_items = find_hit_bullets(["Hadoop"], [db_session.get(ExperienceEntry, position_id)])
    resume_version = confirm_and_finalize(db_session, jd.id, 5, hit_items, [], style_id="default")
    assert resume_version.style_id == "default"
    original_markdown = resume_version.markdown_text
    original_json = resume_version.resume_json

    updated = regenerate_resume_pdf(db_session, resume_version.id, "compact")

    assert updated.id == resume_version.id
    assert updated.style_id == "compact"
    assert updated.markdown_text == original_markdown  # 事实来源完全不受影响
    assert updated.resume_json == original_json
    assert updated.pdf_path is not None
    assert Path(updated.pdf_path).read_bytes()[:4] == b"%PDF"


def test_regenerate_resume_pdf_missing_version_raises(db_session):
    from app.services.resume_tailor import ResumeVersionNotFoundError, regenerate_resume_pdf

    with pytest.raises(ResumeVersionNotFoundError):
        regenerate_resume_pdf(db_session, 9999, "default")


def test_regenerate_resume_pdf_unknown_style_raises_and_keeps_old_pdf(db_session):
    from pathlib import Path

    from app.services.resume_pdf import UnknownResumeStyleError
    from app.services.resume_tailor import regenerate_resume_pdf

    position_id = _seed_experience(db_session)
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Spark experience.")
    hit_items = find_hit_bullets(["Hadoop"], [db_session.get(ExperienceEntry, position_id)])
    resume_version = confirm_and_finalize(db_session, jd.id, 5, hit_items, [], style_id="default")
    original_pdf_path = resume_version.pdf_path

    with pytest.raises(UnknownResumeStyleError):
        regenerate_resume_pdf(db_session, resume_version.id, "does-not-exist")

    db_session.refresh(resume_version)
    assert resume_version.style_id == "default"  # 没被改坏
    assert resume_version.pdf_path == original_pdf_path
    assert Path(resume_version.pdf_path).exists()  # 旧 PDF 文件也还在


def test_regenerate_resume_pdf_propagates_failure_and_keeps_old_state(db_session, monkeypatch):
    """换风格重新渲染失败时（比如 WeasyPrint 依赖当时不可用），不应该悄悄
    什么都不做——这是用户主动点的一个动作,应该让调用方（Dashboard 路由）
    感知到失败并提示用户,同时旧的 style_id/pdf_path 必须原样保留,不能被
    半途写坏。"""
    from app.services import resume_pdf
    from app.services.resume_tailor import regenerate_resume_pdf

    position_id = _seed_experience(db_session)
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Spark experience.")
    hit_items = find_hit_bullets(["Hadoop"], [db_session.get(ExperienceEntry, position_id)])
    resume_version = confirm_and_finalize(db_session, jd.id, 5, hit_items, [], style_id="default")
    original_pdf_path = resume_version.pdf_path

    monkeypatch.setattr(resume_pdf, "_WeasyPrintHTML", None)
    monkeypatch.setattr(resume_pdf, "_WEASYPRINT_IMPORT_ERROR", ImportError("simulated missing libgobject"))

    with pytest.raises(resume_pdf.PdfRenderingUnavailableError):
        regenerate_resume_pdf(db_session, resume_version.id, "compact")

    db_session.refresh(resume_version)
    assert resume_version.style_id == "default"
    assert resume_version.pdf_path == original_pdf_path


# ---------- 回归测试：认领技能库不影响打分引擎 ----------


# ---------- 打磨阶段用户反馈第 1 点：MD 模板驱动的渲染路径 ----------


def test_confirm_and_finalize_with_resume_template_id_renders_via_template(db_session):
    from app.services.resume_pdf import MD_TEMPLATE_STYLE_SENTINEL
    from app.services.resume_template_service import create_template

    position_id = _seed_experience(db_session)
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Spark experience.")
    hit_items = find_hit_bullets(["Hadoop"], [db_session.get(ExperienceEntry, position_id)])

    template = create_template(
        db_session,
        "我的自定义模板",
        "# {{ basic.full_name }}\n\n{% for exp in experience %}## {{ exp.company_name }}\n{% for b in exp.bullet_items %}- {{ b.action_summary }}\n{% endfor %}{% endfor %}",
    )

    resume_version = confirm_and_finalize(
        db_session, jd.id, 5, hit_items, [], style_id="default", resume_template_id=template.id
    )

    assert resume_version.style_id == MD_TEMPLATE_STYLE_SENTINEL
    assert resume_version.resume_template_id == template.id
    assert "## Acme Corp" in resume_version.markdown_text
    assert "Built ETL pipeline" in resume_version.markdown_text
    from pathlib import Path

    assert resume_version.pdf_path is not None
    assert Path(resume_version.pdf_path).read_bytes()[:4] == b"%PDF"


def test_confirm_and_finalize_unknown_resume_template_id_raises_before_inserting(db_session):
    from app.services.resume_template_service import ResumeTemplateNotFoundError

    position_id = _seed_experience(db_session)
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Spark experience.")
    hit_items = find_hit_bullets(["Hadoop"], [db_session.get(ExperienceEntry, position_id)])

    with pytest.raises(ResumeTemplateNotFoundError):
        confirm_and_finalize(db_session, jd.id, 5, hit_items, [], resume_template_id=9999)

    from app.models.tables import ResumeVersion

    assert db_session.query(ResumeVersion).count() == 0


def test_regenerate_resume_pdf_switches_from_style_to_template_and_back(db_session):
    from pathlib import Path

    from app.services.resume_pdf import MD_TEMPLATE_STYLE_SENTINEL
    from app.services.resume_tailor import regenerate_resume_pdf
    from app.services.resume_template_service import create_template

    position_id = _seed_experience(db_session)
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Spark experience.")
    hit_items = find_hit_bullets(["Hadoop"], [db_session.get(ExperienceEntry, position_id)])
    resume_version = confirm_and_finalize(db_session, jd.id, 5, hit_items, [], style_id="default")
    original_markdown = resume_version.markdown_text

    template = create_template(db_session, "模板", "# {{ basic.full_name }}\n\n自定义内容")
    updated = regenerate_resume_pdf(db_session, resume_version.id, resume_template_id=template.id)

    assert updated.style_id == MD_TEMPLATE_STYLE_SENTINEL
    assert updated.resume_template_id == template.id
    assert updated.markdown_text != original_markdown  # 换成模板渲染出来的新内容
    assert "自定义内容" in updated.markdown_text
    assert Path(updated.pdf_path).read_bytes()[:4] == b"%PDF"

    # 切回内置风格：resume_template_id 应该被清空，markdown_text 恢复成
    # _render_markdown(resume_json) 的结果（和最开始一致，因为 resume_json 没变过）。
    back = regenerate_resume_pdf(db_session, resume_version.id, style_id="compact")
    assert back.style_id == "compact"
    assert back.resume_template_id is None
    assert back.markdown_text == original_markdown


def test_regenerate_resume_pdf_unknown_template_id_raises_and_keeps_old_state(db_session):
    from app.services.resume_template_service import ResumeTemplateNotFoundError
    from app.services.resume_tailor import regenerate_resume_pdf

    position_id = _seed_experience(db_session)
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Spark experience.")
    hit_items = find_hit_bullets(["Hadoop"], [db_session.get(ExperienceEntry, position_id)])
    resume_version = confirm_and_finalize(db_session, jd.id, 5, hit_items, [], style_id="default")
    original_style_id = resume_version.style_id
    original_pdf_path = resume_version.pdf_path

    with pytest.raises(ResumeTemplateNotFoundError):
        regenerate_resume_pdf(db_session, resume_version.id, resume_template_id=9999)

    db_session.refresh(resume_version)
    assert resume_version.style_id == original_style_id
    assert resume_version.pdf_path == original_pdf_path


def test_confirm_and_finalize_includes_static_sections_in_resume_json(db_session):
    """个人总结/技能/教育经历/独立项目这些"静态背景信息"应该原样进
    resume_json，且不受 K 值/关键词命中逻辑影响。"""
    from app.services.profile_service import (
        add_education_entry,
        add_personal_project,
        add_personal_project_bullet,
        update_profile_basic,
    )

    position_id = _seed_experience(db_session)
    update_profile_basic(db_session, {"resume_summary": "Line one\nLine two", "skills_text": "Python, Go"})
    add_education_entry(db_session, "MIT", degree="BSc")
    project = add_personal_project(db_session, "Side Bot")
    add_personal_project_bullet(db_session, project.id, "Built a thing")

    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Hadoop experience.")
    hit_items = find_hit_bullets(["Hadoop"], [db_session.get(ExperienceEntry, position_id)])

    resume_version = confirm_and_finalize(db_session, jd.id, 0, hit_items, [])

    resume_json = resume_version.resume_json
    assert resume_json["summary"] == ["Line one", "Line two"]
    assert resume_json["skills"] == ["Python, Go"]
    assert resume_json["education"][0]["school"] == "MIT"
    assert resume_json["projects"][0]["project_name"] == "Side Bot"
    assert resume_json["projects"][0]["bullets"] == ["Built a thing"]
    assert "Line one" in resume_version.markdown_text
    assert "Side Bot" in resume_version.markdown_text


def test_confirm_and_finalize_appends_profile_skills_not_already_in_skills_text(db_session):
    """结构化技能标签（LinkedIn 画像功能新增，见 ProfileSkill 表注释）作为
    补充追加在 skills_text 派生的行之后,已经在 skills_text 里出现过的标签
    不应该重复出现。"""
    from app.services.profile_service import add_profile_skill

    position_id = _seed_experience(db_session)
    update_profile_basic(db_session, {"skills_text": "Python, Go"})
    add_profile_skill(db_session, "Python")  # 已经在 skills_text 里提到过，不应该重复列出
    add_profile_skill(db_session, "Kubernetes")
    add_profile_skill(db_session, "Terraform")

    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Hadoop experience.")
    hit_items = find_hit_bullets(["Hadoop"], [db_session.get(ExperienceEntry, position_id)])
    resume_version = confirm_and_finalize(db_session, jd.id, 0, hit_items, [])

    skills_lines = resume_version.resume_json["skills"]
    assert skills_lines[0] == "Python, Go"
    assert len(skills_lines) == 2
    assert "Kubernetes" in skills_lines[1]
    assert "Terraform" in skills_lines[1]
    assert "Python" not in skills_lines[1]


def test_confirm_and_finalize_uses_profile_skills_when_skills_text_blank(db_session):
    from app.services.profile_service import add_profile_skill

    position_id = _seed_experience(db_session)
    add_profile_skill(db_session, "Rust")
    add_profile_skill(db_session, "Go")

    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Hadoop experience.")
    hit_items = find_hit_bullets(["Hadoop"], [db_session.get(ExperienceEntry, position_id)])
    resume_version = confirm_and_finalize(db_session, jd.id, 0, hit_items, [])

    assert resume_version.resume_json["skills"] == ["Rust, Go"]


def test_claimed_skill_does_not_affect_scoring(db_session):
    position_id = _seed_experience(db_session)
    update_profile_basic(db_session, {"years_experience": 3.0, "education": "Bachelor's degree"})
    jd = create_jd(db_session, company="Beta", title="Eng", description_raw="Need Spark experience.")

    canned_jd_parse = {
        "required_years": 2, "required_education": None, "required_clearance": False,
        "plus_skills": ["Spark"], "core_responsibilities": [], "key_skills": ["Hadoop"],
    }
    canned_score = {
        "skill_fit_score": 70, "matched_key_skills": ["Hadoop"],
        "meets_education_requirement": True, "meets_clearance_requirement": True,
        "plus_skills_matched": [], "strengths": ["经验对口"], "weaknesses": ["缺 Spark 经验"],
    }

    # 第一次打分：还没有任何认领技能。JD 分析（解析+打分）两步都走轻量模型
    # 槽位（打磨阶段用户反馈修复，见 analysis.analyze_jd 的说明），按调用
    # 顺序预置两条响应。
    light1 = FakeLLMClient(responses=[dict(canned_jd_parse), dict(canned_score)])
    score_before = analyze_jd(db_session, jd.id, light1)
    total_before = score_before.total_score

    # 认领一条 Spark 技能（模拟用户在简历重制里确认了这条延伸建议）
    confirm_and_finalize(
        db_session, jd.id, 8, [],
        [{"keyword": "Spark", "experience_entry_id": position_id,
          "action_summary": "Used Spark", "result_summary": "improved throughput", "rationale": "r"}],
    )
    assert db_session.query(ClaimedSkill).count() == 1

    # 用完全相同的 canned 输入重新打分：分数必须和之前一模一样，
    # 不能因为多了一条 claimed_skill 就变化。
    light2 = FakeLLMClient(responses=[dict(canned_jd_parse), dict(canned_score)])
    score_after = analyze_jd(db_session, jd.id, light2)

    assert score_after.total_score == total_before
    assert score_after.skill_fit_score == score_before.skill_fit_score
    assert score_after.breakdown == score_before.breakdown
