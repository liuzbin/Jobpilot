"""Phase 2：画像深化——bullet 三元组抽取 + 追问式访谈（app/services/profile_deepening.py）。"""

from __future__ import annotations

import pytest

from app.core.llm_client import FakeLLMClient
from app.models.tables import ExperienceEntry, ExperienceLevel
from app.services.profile_deepening import (
    backfill_bullet_triads,
    extract_bullet_triads,
    generate_background_questions,
    merge_background_answers,
)
from app.services.profile_service import merge_parsed_experience


def _seed_one_bullet(db_session, company="Acme Corp", bullets=None) -> int:
    parsed = {
        "basic": {},
        "companies": [
            {
                "company_name": company,
                "positions": [
                    {
                        "position_title": "Data Engineer",
                        "project_name": "Big Data Platform",
                        "start_date": "2022-01",
                        "end_date": None,
                        "is_current": True,
                        "bullets": bullets or ["Built a Hadoop-based ETL pipeline processing 2TB of logs daily"],
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
    return position.id


def test_extract_bullet_triads_matches_input_order_and_length(db_session):
    fake = FakeLLMClient(
        responses=[
            {
                "triads": [
                    {"keywords": ["Hadoop"], "action_summary": "Built ETL", "result_summary": "Faster"},
                    {"keywords": ["Python"], "action_summary": "Wrote scripts", "result_summary": None},
                ]
            }
        ]
    )
    triads = extract_bullet_triads(["bullet one", "bullet two"], fake)
    assert len(triads) == 2
    assert triads[0]["keywords"] == ["Hadoop"]
    assert triads[1]["result_summary"] is None


def test_extract_bullet_triads_defensively_pads_when_model_returns_too_few():
    """模型返回的 triad 数量少于输入 bullet 数量时,不应该抛异常或丢数据,
    多出来的 bullet 用空 triad 兜底。"""
    fake = FakeLLMClient(responses=[{"triads": [{"keywords": ["A"], "action_summary": "x", "result_summary": None}]}])
    triads = extract_bullet_triads(["bullet one", "bullet two", "bullet three"], fake)
    assert len(triads) == 3
    assert triads[0]["keywords"] == ["A"]
    assert triads[1] == {"keywords": [], "action_summary": None, "result_summary": None}
    assert triads[2] == {"keywords": [], "action_summary": None, "result_summary": None}


def test_extract_bullet_triads_empty_input_returns_empty_without_calling_llm():
    fake = FakeLLMClient(responses=[])
    assert extract_bullet_triads([], fake) == []
    assert fake.calls == []


def test_backfill_bullet_triads_only_touches_bullets_without_keywords(db_session):
    _seed_one_bullet(db_session)
    fake = FakeLLMClient(
        responses=[{"triads": [{"keywords": ["Hadoop", "ETL"], "action_summary": "Built ETL", "result_summary": "Faster"}]}]
    )
    updated = backfill_bullet_triads(db_session, fake)
    assert updated == 1

    # 再跑一次：已经有 keywords 的 bullet 不应该被重新处理，FakeLLMClient 没有
    # 更多预设响应，如果被多调用一次就会抛 LLMCallError。
    fake_empty = FakeLLMClient(responses=[])
    updated_again = backfill_bullet_triads(db_session, fake_empty)
    assert updated_again == 0


def test_generate_background_questions_filters_non_string_entries(db_session):
    position_id = _seed_one_bullet(db_session)
    position = db_session.get(ExperienceEntry, position_id)
    fake = FakeLLMClient(responses=[{"questions": ["数据规模大概多大？", "", "  ", 123, "你负责哪一层？"]}])
    questions = generate_background_questions(position, fake)
    assert questions == ["数据规模大概多大？", "你负责哪一层？"]


def test_merge_background_answers_skips_blank_answers_and_persists_qa(db_session):
    position_id = _seed_one_bullet(db_session)
    fake = FakeLLMClient(responses=[{"background_notes": "整理后的背景描述"}])
    qa_pairs = [
        {"question": "数据规模多大？", "answer": "2TB/天"},
        {"question": "你负责哪一层？", "answer": "   "},  # 空白答案视为跳过
    ]
    position = merge_background_answers(db_session, position_id, qa_pairs, fake)

    assert position.background_notes == "整理后的背景描述"
    assert position.background_qa == [{"question": "数据规模多大？", "answer": "2TB/天"}]


def test_merge_background_answers_all_blank_does_not_call_llm(db_session):
    position_id = _seed_one_bullet(db_session)
    fake = FakeLLMClient(responses=[])
    qa_pairs = [{"question": "数据规模多大？", "answer": "  "}]
    position = merge_background_answers(db_session, position_id, qa_pairs, fake)
    assert position.background_notes is None
    assert fake.calls == []


def test_merge_background_answers_accumulates_across_rounds(db_session):
    position_id = _seed_one_bullet(db_session)
    fake1 = FakeLLMClient(responses=[{"background_notes": "第一轮整理"}])
    merge_background_answers(db_session, position_id, [{"question": "Q1", "answer": "A1"}], fake1)

    fake2 = FakeLLMClient(responses=[{"background_notes": "第二轮整理，包含更多信息"}])
    position = merge_background_answers(db_session, position_id, [{"question": "Q2", "answer": "A2"}], fake2)

    assert position.background_notes == "第二轮整理，包含更多信息"
    assert position.background_qa == [
        {"question": "Q1", "answer": "A1"},
        {"question": "Q2", "answer": "A2"},
    ]


def test_merge_background_answers_rejects_non_position_entry(db_session):
    position_id = _seed_one_bullet(db_session)
    position = db_session.get(ExperienceEntry, position_id)
    company_id = position.parent_id
    fake = FakeLLMClient(responses=[])
    with pytest.raises(ValueError):
        merge_background_answers(db_session, company_id, [{"question": "Q", "answer": "A"}], fake)
