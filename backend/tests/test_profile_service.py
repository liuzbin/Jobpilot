"""Phase 1：画像合并逻辑（profile_service.merge_parsed_experience 等）。

覆盖 profile_service.py 顶部注释里写的合并规则：
- 公司/项目按名称不区分大小写模糊匹配，匹配上复用、匹配不上新增；
- 贡献句按去除首尾空白后的精确文本去重；
- 简历抽取的基本信息只填空字段，不覆盖用户已经手动填过的字段；
- Dashboard 表单提交（update_profile_basic）是整体覆盖语义。
"""

from __future__ import annotations

from app.models.tables import ExperienceEntry, ExperienceLevel
from app.services.profile_service import (
    fill_blank_profile_basic_fields,
    get_experience_tree,
    get_or_create_profile_basic,
    merge_parsed_experience,
    update_profile_basic,
)


def _parsed(company_name="Acme Corp", position_title="Engineer", project_name="Search", bullets=None):
    return {
        "basic": {},
        "companies": [
            {
                "company_name": company_name,
                "positions": [
                    {
                        "position_title": position_title,
                        "project_name": project_name,
                        "start_date": "2022-01",
                        "end_date": None,
                        "is_current": True,
                        "bullets": bullets if bullets is not None else ["Did a thing", "Did another thing"],
                    }
                ],
            }
        ],
    }


def test_merge_creates_new_company_position_and_bullets(db_session):
    result = merge_parsed_experience(db_session, _parsed())

    assert result.companies_added == 1
    assert result.positions_added == 1
    assert result.bullets_added == 2
    assert result.bullets_skipped_duplicate == 0

    tree = get_experience_tree(db_session)
    assert len(tree) == 1
    assert tree[0]["company_name"] == "Acme Corp"
    assert len(tree[0]["positions"]) == 1
    assert [b["content"] for b in tree[0]["positions"][0]["bullets"]] == [
        "Did a thing",
        "Did another thing",
    ]


def test_merge_matches_existing_company_case_insensitively(db_session):
    merge_parsed_experience(db_session, _parsed(company_name="Acme Corp"))
    result = merge_parsed_experience(db_session, _parsed(company_name="  acme corp  ", bullets=["A new bullet"]))

    # 第二次不应该新建公司/岗位，只应该新增一条贡献句
    assert result.companies_added == 0
    assert result.positions_added == 0
    assert result.bullets_added == 1

    tree = get_experience_tree(db_session)
    assert len(tree) == 1  # 仍然只有一家公司
    assert len(tree[0]["positions"]) == 1
    bullets = [b["content"] for b in tree[0]["positions"][0]["bullets"]]
    assert bullets == ["Did a thing", "Did another thing", "A new bullet"]


def test_merge_skips_duplicate_bullets_by_normalized_text(db_session):
    merge_parsed_experience(db_session, _parsed())
    # 完全相同的内容再来一次（前后加空白），应该被当成重复跳过
    result = merge_parsed_experience(
        db_session, _parsed(bullets=["  Did a thing  ", "Did another thing"])
    )

    assert result.bullets_added == 0
    assert result.bullets_skipped_duplicate == 2

    tree = get_experience_tree(db_session)
    bullets = [b["content"] for b in tree[0]["positions"][0]["bullets"]]
    assert len(bullets) == 2  # 没有重复插入


def test_merge_adds_new_position_under_existing_company(db_session):
    merge_parsed_experience(db_session, _parsed(position_title="Engineer", project_name="Search"))
    result = merge_parsed_experience(
        db_session, _parsed(position_title="Senior Engineer", project_name="Payments")
    )

    assert result.companies_added == 0
    assert result.positions_added == 1

    tree = get_experience_tree(db_session)
    assert len(tree[0]["positions"]) == 2


def test_fill_blank_profile_basic_fields_does_not_overwrite_existing_values(db_session):
    update_profile_basic(db_session, {"full_name": "Alice", "email": None})

    filled = fill_blank_profile_basic_fields(
        db_session, {"full_name": "Bob (from resume)", "email": "alice@example.com", "school": "MIT"}
    )

    profile = get_or_create_profile_basic(db_session)
    # full_name 已经有值（用户手动填过），不能被简历解析结果覆盖
    assert profile.full_name == "Alice"
    # email 之前是 None，应该被填充
    assert profile.email == "alice@example.com"
    assert profile.school == "MIT"
    assert set(filled) == {"email", "school"}


def test_merge_fills_basic_fields_via_parsed_basic(db_session):
    parsed = _parsed()
    parsed["basic"] = {"full_name": "Carol", "school": "Waterloo"}
    result = merge_parsed_experience(db_session, parsed)

    assert set(result.basic_fields_filled) == {"full_name", "school"}
    profile = get_or_create_profile_basic(db_session)
    assert profile.full_name == "Carol"
    assert profile.school == "Waterloo"


def test_update_profile_basic_overwrites_existing_value(db_session):
    update_profile_basic(db_session, {"full_name": "Alice"})
    update_profile_basic(db_session, {"full_name": "Alice Updated"})

    profile = get_or_create_profile_basic(db_session)
    assert profile.full_name == "Alice Updated"


def test_merge_ignores_company_entries_with_blank_name(db_session):
    parsed = _parsed()
    parsed["companies"].append({"company_name": "  ", "positions": []})
    result = merge_parsed_experience(db_session, parsed)

    assert result.companies_added == 1  # 空名字的那条被跳过，没有算进去
    tree = get_experience_tree(db_session)
    assert len(tree) == 1


def test_get_experience_tree_orders_by_order_index(db_session):
    merge_parsed_experience(db_session, _parsed(company_name="First Co"))
    merge_parsed_experience(db_session, _parsed(company_name="Second Co"))

    tree = get_experience_tree(db_session)
    assert [c["company_name"] for c in tree] == ["First Co", "Second Co"]


def test_experience_entry_level_enum_roundtrip(db_session):
    merge_parsed_experience(db_session, _parsed())
    companies = (
        db_session.query(ExperienceEntry).filter(ExperienceEntry.level == ExperienceLevel.COMPANY).all()
    )
    assert len(companies) == 1
    assert companies[0].children[0].level == ExperienceLevel.POSITION
