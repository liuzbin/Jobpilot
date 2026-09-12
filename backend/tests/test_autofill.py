"""Phase 4：自动化填表决策逻辑（app/services/autofill.py）。

重点覆盖两条安全防线：合规/法律声明性质的选择类控件（工作授权、EEO、
背景调查等）无论标签映射结果是什么都必须跳过，不能被自动选择。
"""

from __future__ import annotations

from app.core.llm_client import FakeLLMClient
from app.services.autofill import build_autofill_plan
from app.services.profile_service import update_profile_basic
from app.services.qa_bank_service import add_qa_entry


def _field(field_id, label, input_type="text", options=None):
    return {"field_id": field_id, "label": label, "input_type": input_type, "options": options or []}


def test_keyword_match_fills_text_field_from_profile(db_session):
    update_profile_basic(db_session, {"email": "a@example.com"})
    fields = [_field("f1", "Email Address", "email")]
    plan = build_autofill_plan(db_session, fields, light_client=None)
    assert len(plan) == 1
    assert plan[0].action == "fill"
    assert plan[0].value == "a@example.com"
    assert plan[0].source == "profile"


def test_keyword_match_skips_when_profile_field_empty(db_session):
    fields = [_field("f1", "Phone", "tel")]
    plan = build_autofill_plan(db_session, fields, light_client=None)
    assert plan[0].action == "skip"
    assert plan[0].reason == "profile_field_empty"


def test_first_and_last_name_split_from_full_name(db_session):
    update_profile_basic(db_session, {"full_name": "Zhang Wei"})
    fields = [_field("f1", "First Name"), _field("f2", "Last Name")]
    plan = build_autofill_plan(db_session, fields, light_client=None)
    by_id = {a.field_id: a for a in plan}
    assert by_id["f1"].value == "Zhang"
    assert by_id["f2"].value == "Wei"


def test_compliance_choice_field_skipped_even_without_llm(db_session):
    update_profile_basic(db_session, {"work_authorization": "US Citizen"})
    fields = [
        _field(
            "f1",
            "Are you legally authorized to work in the US?",
            "radio",
            [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}],
        )
    ]
    plan = build_autofill_plan(db_session, fields, light_client=None)
    assert plan[0].action == "skip"
    assert plan[0].reason == "compliance_sensitive_choice_control"


def test_work_authorization_free_text_is_allowed(db_session):
    update_profile_basic(db_session, {"work_authorization": "US Citizen"})
    fields = [_field("f1", "Work authorization status", "text")]
    plan = build_autofill_plan(db_session, fields, light_client=None)
    assert plan[0].action == "fill"
    assert plan[0].value == "US Citizen"


def test_llm_mapped_compliance_field_on_select_still_skipped(db_session):
    # 即使 LLM 把这个陌生标签映射成了 work_authorization,选择类控件也不在
    # 白名单里,必须跳过——这是防御性的第二层，独立于关键词兜底扫描。
    update_profile_basic(db_session, {"work_authorization": "US Citizen"})
    fake = FakeLLMClient(
        responses=[{"mappings": [{"index": 0, "field_key": "work_authorization", "is_essay_question": False}]}]
    )
    fields = [
        _field(
            "f1",
            "Sponsorship needed for employment?",
            "select",
            [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}],
        )
    ]
    plan = build_autofill_plan(db_session, fields, light_client=fake)
    assert plan[0].action == "skip"


def test_safe_choice_field_can_be_auto_selected(db_session):
    update_profile_basic(db_session, {"current_location": "Toronto, ON"})
    fields = [
        _field(
            "f1",
            "Current Location",
            "select",
            [{"value": "toronto", "label": "Toronto, ON"}],
        )
    ]
    plan = build_autofill_plan(db_session, fields, light_client=None)
    assert plan[0].action == "select"
    assert plan[0].value == "Toronto, ON"


def test_unsafe_choice_field_mapped_by_keyword_still_skipped(db_session):
    # education 不在 SAFE_CHOICE_FIELD_KEYS 白名单里，即使关键词命中了
    # education 这个 field_key，选择类控件也必须跳过。
    update_profile_basic(db_session, {"education": "Master"})
    fields = [
        _field("f1", "Highest Degree", "select", [{"value": "ms", "label": "Master"}]),
    ]
    plan = build_autofill_plan(db_session, fields, light_client=None)
    assert plan[0].action == "skip"
    assert plan[0].reason == "field_not_allowed_for_choice_control"


def test_essay_question_matched_via_qa_bank(db_session):
    add_qa_entry(db_session, "请简单介绍一下你自己", "我是一名后端工程师")
    fake = FakeLLMClient(
        responses=[{"mappings": [{"index": 0, "field_key": None, "is_essay_question": True}]}]
    )
    fields = [_field("f1", "请简单地自我介绍一下", "textarea")]
    plan = build_autofill_plan(db_session, fields, light_client=fake)
    assert plan[0].action == "fill"
    assert plan[0].source == "qa_bank"
    assert plan[0].value == "我是一名后端工程师"


def test_essay_question_without_qa_bank_match_is_unmapped(db_session):
    fake = FakeLLMClient(
        responses=[{"mappings": [{"index": 0, "field_key": None, "is_essay_question": True}]}]
    )
    fields = [_field("f1", "一道完全陌生的问题", "textarea")]
    plan = build_autofill_plan(db_session, fields, light_client=fake)
    assert plan[0].action == "skip"
    assert plan[0].reason == "no_similar_qa_bank_entry"


def test_unmapped_field_without_llm_client_is_skipped(db_session):
    fields = [_field("f1", "一道完全陌生的标签", "text")]
    plan = build_autofill_plan(db_session, fields, light_client=None)
    assert plan[0].action == "skip"
    assert plan[0].reason == "unmapped"


def test_llm_failure_degrades_to_unmapped_without_raising(db_session):
    class ExplodingClient:
        def complete_json(self, system_prompt, user_prompt):
            raise RuntimeError("network exploded")

    fields = [_field("f1", "一道完全陌生的标签", "text")]
    plan = build_autofill_plan(db_session, fields, light_client=ExplodingClient())
    assert plan[0].action == "skip"
    assert plan[0].reason == "unmapped"


def test_llm_returns_unknown_field_key_is_ignored(db_session):
    fake = FakeLLMClient(
        responses=[{"mappings": [{"index": 0, "field_key": "not_a_real_field", "is_essay_question": False}]}]
    )
    fields = [_field("f1", "一道完全陌生的标签", "text")]
    plan = build_autofill_plan(db_session, fields, light_client=fake)
    assert plan[0].action == "skip"
    assert plan[0].reason == "unmapped"


def test_duplicate_labels_with_different_field_ids_resolved_independently(db_session):
    # 回归测试：两个字段标签、类型完全相同但 field_id 不同时，不能因为
    # dataclass 按值比较相等就被 LLM 映射结果串位（内部用 field_id 建索引
    # 映射，而不是用 list.index() 反查 dataclass 实例）。
    fake = FakeLLMClient(
        responses=[
            {
                "mappings": [
                    {"index": 0, "field_key": "email", "is_essay_question": False},
                    {"index": 1, "field_key": None, "is_essay_question": False},
                ]
            }
        ]
    )
    update_profile_basic(db_session, {"email": "dup@example.com"})
    fields = [
        _field("f1", "一个陌生标签", "text"),
        _field("f2", "一个陌生标签", "text"),
    ]
    plan = build_autofill_plan(db_session, fields, light_client=fake)
    by_id = {a.field_id: a for a in plan}
    assert by_id["f1"].action == "fill"
    assert by_id["f1"].value == "dup@example.com"
    assert by_id["f2"].action == "skip"
    assert by_id["f2"].reason == "unmapped"


def test_batches_multiple_unresolved_labels_into_single_llm_call(db_session):
    # 两个标签都必须真的靠不上关键词兜底（否则根本不会进入 LLM 批量映射,
    # 也就没法验证"合并成一次调用"这件事），所以故意用两个关键词表都覆盖
    # 不到的说法。
    fake = FakeLLMClient(
        responses=[
            {
                "mappings": [
                    {"index": 0, "field_key": "github_url", "is_essay_question": False},
                    {"index": 1, "field_key": None, "is_essay_question": True},
                ]
            }
        ]
    )
    update_profile_basic(db_session, {"github_url": "https://github.com/x"})
    add_qa_entry(db_session, "你为什么想加入这个行业", "因为热爱技术")
    fields = [
        _field("f1", "在线代码仓库链接", "text"),
        _field("f2", "你为什么想加入这个行业？", "textarea"),
    ]
    plan = build_autofill_plan(db_session, fields, light_client=fake)
    assert len(fake.calls) == 1  # 两个未命中的字段应该合并成一次调用
    by_id = {a.field_id: a for a in plan}
    assert by_id["f1"].value == "https://github.com/x"
    assert by_id["f2"].source == "qa_bank"
