"""Phase 2 补完：题库问答收集流程（app/services/qa_bank_service.py）。
Phase 4 补充：写入时自动落库 embedding、历史数据回填、相似度检索。"""

from __future__ import annotations

from app.core.llm_client import FakeLLMClient
from app.models.tables import ProfileBasic, QABankEntry, QASource
from app.services.profile_service import get_or_create_profile_basic, update_profile_basic
from app.services.qa_bank_service import (
    add_qa_entry,
    backfill_qa_embeddings,
    delete_qa_entry,
    find_similar_answer,
    generate_common_qa_questions,
    list_qa_entries,
    save_qa_answers,
)
from app.services.qa_similarity import bytes_to_embedding


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


def test_add_qa_entry_computes_and_stores_embedding(db_session):
    entry = add_qa_entry(db_session, "请介绍一下你自己", "我是一名数据工程师")
    assert entry.embedding is not None
    vector = bytes_to_embedding(entry.embedding)
    assert vector.any()


def test_add_qa_entry_update_recomputes_embedding(db_session):
    add_qa_entry(db_session, "你的职业规划是什么", "第一版答案")
    updated = add_qa_entry(db_session, "你的职业规划是什么", "第二版，完全不同的内容更长一些")
    assert updated.embedding is not None


def test_backfill_qa_embeddings_fills_legacy_null_entries(db_session):
    # 模拟 Phase 2 时代写入、还没有 embedding 的历史记录：直接绕过
    # add_qa_entry 手工插入一条 embedding=None 的记录。
    legacy = QABankEntry(
        question_text="历史遗留问题",
        answer_text="历史答案",
        source=QASource.ONBOARDING,
        embedding=None,
    )
    db_session.add(legacy)
    db_session.commit()

    filled = backfill_qa_embeddings(db_session)
    assert filled == 1

    db_session.refresh(legacy)
    assert legacy.embedding is not None


def test_backfill_qa_embeddings_is_noop_when_nothing_missing(db_session):
    add_qa_entry(db_session, "问题一", "答案一")
    assert backfill_qa_embeddings(db_session) == 0


def test_find_similar_answer_returns_best_match_above_threshold(db_session):
    add_qa_entry(db_session, "请简单介绍一下你自己", "我是一名后端工程师，专注分布式系统")
    add_qa_entry(db_session, "你期望的薪资范围是多少", "面议")

    match, score = find_similar_answer(db_session, "请简单地自我介绍一下")
    assert match is not None
    assert match.question_text == "请简单介绍一下你自己"
    assert score > 0.3


def test_find_similar_answer_returns_none_when_below_threshold(db_session):
    add_qa_entry(db_session, "你期望的薪资范围是多少", "面议")
    match, score = find_similar_answer(db_session, "你会哪些编程语言")
    assert match is None


def test_find_similar_answer_empty_qa_bank_returns_none(db_session):
    match, score = find_similar_answer(db_session, "任意问题")
    assert match is None
    assert score == 0.0


def test_find_similar_answer_blank_question_returns_none(db_session):
    add_qa_entry(db_session, "问题一", "答案一")
    match, score = find_similar_answer(db_session, "   ")
    assert match is None
    assert score == 0.0
