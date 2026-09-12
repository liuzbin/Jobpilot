"""Phase 2 补完：题库问答收集流程（app/services/qa_bank_service.py）。"""

from __future__ import annotations

from app.core.llm_client import FakeLLMClient
from app.models.tables import ProfileBasic, QABankEntry, QASource
from app.services.profile_service import get_or_create_profile_basic, update_profile_basic
from app.services.qa_bank_service import (
    add_qa_entry,
    delete_qa_entry,
    generate_common_qa_questions,
    list_qa_entries,
    save_qa_answers,
)


def test_generate_common_qa_questions_filters_blank_and_non_string(db_session):
    update_profile_basic(db_session, {"target_title": "数据工程师"})
    profile = get_or_create_profile_basic(db_session)
    fake = FakeLLMClient(
        responses=[{"questions": ["请介绍一下你自己", "", "  ", 123, "你最大的优势是什么？"]}]
    )
    questions = generate_common_qa_questions(profile, fake)
    assert questions == ["请介绍一下你自己", "你最大的优势是什么？"]


def test_generate_common_qa_questions_handles_missing_target_title(db_session):
    profile = get_or_create_profile_basic(db_session)
    fake = FakeLLMClient(responses=[{"questions": ["请介绍一下你自己"]}])
    questions = generate_common_qa_questions(profile, fake)
    assert questions == ["请介绍一下你自己"]
    # 目标职位缺失时也不应该报错，prompt 里会用占位文案兜底
    assert "未填写目标职位" in fake.calls[0][1]


def test_add_qa_entry_skips_blank_answer(db_session):
    entry = add_qa_entry(db_session, "请介绍一下你自己", "   ")
    assert entry is None
    assert db_session.query(QABankEntry).count() == 0


def test_add_qa_entry_skips_blank_question(db_session):
    entry = add_qa_entry(db_session, "   ", "我是一名数据工程师")
    assert entry is None
    assert db_session.query(QABankEntry).count() == 0


def test_add_qa_entry_upserts_by_normalized_question_text(db_session):
    add_qa_entry(db_session, "请介绍一下你自己", "第一版答案")
    add_qa_entry(db_session, "  请介绍一下你自己  ", "第二版答案，更完整")

    entries = db_session.query(QABankEntry).all()
    assert len(entries) == 1
    assert entries[0].answer_text == "第二版答案，更完整"


def test_add_qa_entry_default_source_is_onboarding(db_session):
    entry = add_qa_entry(db_session, "你最大的优势是什么？", "学习能力强")
    assert entry.source == QASource.ONBOARDING


def test_save_qa_answers_skips_blank_and_returns_saved_count(db_session):
    qa_pairs = [
        {"question": "请介绍一下你自己", "answer": "我是一名数据工程师"},
        {"question": "你的职业规划是什么？", "answer": "   "},  # 空白答案跳过
    ]
    saved = save_qa_answers(db_session, qa_pairs)
    assert saved == 1
    assert db_session.query(QABankEntry).count() == 1


def test_list_qa_entries_orders_newest_first(db_session):
    add_qa_entry(db_session, "问题一", "答案一")
    add_qa_entry(db_session, "问题二", "答案二")
    entries = list_qa_entries(db_session)
    assert [e.question_text for e in entries] == ["问题二", "问题一"]


def test_delete_qa_entry_removes_existing(db_session):
    entry = add_qa_entry(db_session, "问题一", "答案一")
    assert delete_qa_entry(db_session, entry.id) is True
    assert db_session.query(QABankEntry).count() == 0


def test_delete_qa_entry_missing_returns_false(db_session):
    assert delete_qa_entry(db_session, 9999) is False
