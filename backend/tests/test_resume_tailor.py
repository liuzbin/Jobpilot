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
    build_resume_draft,
    collect_jd_keywords,
    confirm_and_finalize,
    find_hit_bullets,
    find_missing_keywords,
    generate_extension_suggestions,
    rewrite_hit_bullets,
    select_keywords_to_extend,
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
        responses=[{"required_years": None, "required_education": None, "required_clearance": False,
                     "plus_skills": [], "core_responsibilities": [], "key_skills": ["Hadoop", "Spark"]}]
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
    assert resume_version.pdf_path is None  # PDF 渲染留待后续增量

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


# ---------- 回归测试：认领技能库不影响打分引擎 ----------


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

    # 第一次打分：还没有任何认领技能
    light1 = FakeLLMClient(responses=[dict(canned_jd_parse)])
    heavy1 = FakeLLMClient(responses=[dict(canned_score)])
    score_before = analyze_jd(db_session, jd.id, light1, heavy1)
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
    light2 = FakeLLMClient(responses=[dict(canned_jd_parse)])
    heavy2 = FakeLLMClient(responses=[dict(canned_score)])
    score_after = analyze_jd(db_session, jd.id, light2, heavy2)

    assert score_after.total_score == total_before
    assert score_after.skill_fit_score == score_before.skill_fit_score
    assert score_after.breakdown == score_before.breakdown
