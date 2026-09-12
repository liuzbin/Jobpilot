"""Phase 1：画像合并逻辑（profile_service.merge_parsed_experience 等）。

覆盖 profile_service.py 顶部注释里写的合并规则：
- 公司/项目按名称不区分大小写模糊匹配，匹配上复用、匹配不上新增；
- 贡献句按去除首尾空白后的精确文本去重；
- 简历抽取的基本信息只填空字段，不覆盖用户已经手动填过的字段；
- Dashboard 表单提交（update_profile_basic）是整体覆盖语义。
"""

from __future__ import annotations

import pytest

from app.models.tables import ExperienceBullet, ExperienceEntry, ExperienceLevel
from app.services.profile_service import (
    add_bullet,
    add_company,
    add_position,
    delete_bullet,
    delete_company,
    delete_position,
    fill_blank_profile_basic_fields,
    get_experience_tree,
    get_or_create_profile_basic,
    merge_parsed_experience,
    resolve_bullet_conflict,
    resolve_position_field,
    update_bullet_content,
    update_position_fields,
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


# ---------- Phase 2 补完：合并冲突检测（近似重复的贡献句 / 职位字段冲突） ----------


def test_merge_flags_near_duplicate_bullet_as_conflict_instead_of_auto_adding(db_session):
    merge_parsed_experience(db_session, _parsed(bullets=["Built a Hadoop-based ETL pipeline processing logs"]))
    result = merge_parsed_experience(
        db_session,
        _parsed(bullets=["Built a Hadoop based ETL pipeline for processing logs"]),  # 高度相似，措辞略有不同
    )

    assert result.bullets_added == 0
    assert result.bullets_skipped_duplicate == 0
    assert len(result.bullet_conflicts) == 1
    conflict = result.bullet_conflicts[0]
    assert conflict["existing_text"] == "Built a Hadoop-based ETL pipeline processing logs"
    assert conflict["new_text"] == "Built a Hadoop based ETL pipeline for processing logs"
    assert conflict["similarity"] >= 0.82

    # 冲突没有被自动应用：画像里仍然只有原来那一条
    tree = get_experience_tree(db_session)
    bullets = [b["content"] for b in tree[0]["positions"][0]["bullets"]]
    assert bullets == ["Built a Hadoop-based ETL pipeline processing logs"]


def test_merge_does_not_flag_genuinely_different_bullets_as_conflict(db_session):
    merge_parsed_experience(db_session, _parsed(bullets=["Built a Hadoop-based ETL pipeline"]))
    result = merge_parsed_experience(db_session, _parsed(bullets=["Led a cross-team migration to Kubernetes"]))

    assert result.bullet_conflicts == []
    assert result.bullets_added == 1


def test_merge_flags_position_date_conflict_when_existing_value_differs(db_session):
    merge_parsed_experience(db_session, _parsed())  # start_date="2022-01"
    parsed2 = _parsed(bullets=[])
    parsed2["companies"][0]["positions"][0]["start_date"] = "2021-06"
    result = merge_parsed_experience(db_session, parsed2)

    assert len(result.position_field_conflicts) == 1
    conflict = result.position_field_conflicts[0]
    assert conflict["field"] == "start_date"
    assert conflict["old_value"] == "2022-01"
    assert conflict["new_value"] == "2021-06"

    # 冲突没有被自动应用
    tree = get_experience_tree(db_session)
    assert tree[0]["positions"][0]["start_date"] == "2022-01"


def test_merge_fills_blank_position_date_without_conflict(db_session):
    parsed1 = _parsed()
    parsed1["companies"][0]["positions"][0]["start_date"] = None
    merge_parsed_experience(db_session, parsed1)

    parsed2 = _parsed(bullets=[])
    parsed2["companies"][0]["positions"][0]["start_date"] = "2022-01"
    result = merge_parsed_experience(db_session, parsed2)

    assert result.position_field_conflicts == []  # 填空不算冲突
    tree = get_experience_tree(db_session)
    assert tree[0]["positions"][0]["start_date"] == "2022-01"


def test_merge_flags_is_current_conflict_only_when_incoming_is_true(db_session):
    parsed1 = _parsed()
    parsed1["companies"][0]["positions"][0]["is_current"] = False
    merge_parsed_experience(db_session, parsed1)

    parsed2 = _parsed(bullets=[])
    parsed2["companies"][0]["positions"][0]["is_current"] = True
    result = merge_parsed_experience(db_session, parsed2)

    assert len(result.position_field_conflicts) == 1
    assert result.position_field_conflicts[0]["field"] == "is_current"


# ---------- Phase 2 补完：合并冲突确认（用户选择之后应用） ----------


def test_resolve_position_field_applies_new_value(db_session):
    merge_parsed_experience(db_session, _parsed())
    position = db_session.query(ExperienceEntry).filter(ExperienceEntry.level == ExperienceLevel.POSITION).one()

    resolve_position_field(db_session, position.id, "start_date", "2021-06")

    db_session.refresh(position)
    assert position.start_date == "2021-06"


def test_resolve_bullet_conflict_keep_old_does_nothing(db_session):
    merge_parsed_experience(db_session, _parsed(bullets=["Original bullet text here"]))
    position = db_session.query(ExperienceEntry).filter(ExperienceEntry.level == ExperienceLevel.POSITION).one()
    existing_bullet = position.bullets[0]

    resolve_bullet_conflict(db_session, position.id, "keep_old", existing_bullet.id, "New rewritten text here")

    db_session.refresh(existing_bullet)
    assert existing_bullet.content == "Original bullet text here"
    assert len(position.bullets) == 1


def test_resolve_bullet_conflict_replace_updates_content_and_resets_triad(db_session):
    merge_parsed_experience(db_session, _parsed(bullets=["Original bullet text here"]))
    position = db_session.query(ExperienceEntry).filter(ExperienceEntry.level == ExperienceLevel.POSITION).one()
    existing_bullet = position.bullets[0]
    existing_bullet.keywords = ["Old"]
    existing_bullet.action_summary = "old summary"
    db_session.commit()

    resolve_bullet_conflict(db_session, position.id, "replace", existing_bullet.id, "New rewritten text here")

    db_session.refresh(existing_bullet)
    assert existing_bullet.content == "New rewritten text here"
    assert existing_bullet.keywords is None
    assert existing_bullet.action_summary is None


def test_resolve_bullet_conflict_keep_both_adds_new_bullet(db_session):
    merge_parsed_experience(db_session, _parsed(bullets=["Original bullet text here"]))
    position = db_session.query(ExperienceEntry).filter(ExperienceEntry.level == ExperienceLevel.POSITION).one()
    existing_bullet = position.bullets[0]

    resolve_bullet_conflict(db_session, position.id, "keep_both", existing_bullet.id, "New rewritten text here")

    db_session.refresh(position)
    contents = {b.content for b in position.bullets}
    assert contents == {"Original bullet text here", "New rewritten text here"}


# ---------- Phase 2 补完：工作经历树的手动增删改 ----------


def test_add_company_appends_with_incrementing_order(db_session):
    c1 = add_company(db_session, "First Co")
    c2 = add_company(db_session, "Second Co")
    assert c2.order_index > c1.order_index


def test_add_company_rejects_blank_name(db_session):
    with pytest.raises(ValueError):
        add_company(db_session, "   ")


def test_add_position_and_update_fields(db_session):
    company = add_company(db_session, "Acme Corp")
    position = add_position(db_session, company.id, position_title="Engineer", start_date="2022-01")
    assert position.parent_id == company.id
    assert position.position_title == "Engineer"

    updated = update_position_fields(db_session, position.id, {"position_title": "Senior Engineer", "is_current": True})
    assert updated.position_title == "Senior Engineer"
    assert updated.is_current is True


def test_add_position_rejects_unknown_company(db_session):
    with pytest.raises(ValueError):
        add_position(db_session, 9999, position_title="Engineer")


def test_add_bullet_and_update_content_resets_triad(db_session):
    company = add_company(db_session, "Acme Corp")
    position = add_position(db_session, company.id, position_title="Engineer")
    bullet = add_bullet(db_session, position.id, "Did a great thing")
    bullet.keywords = ["X"]
    db_session.commit()

    updated = update_bullet_content(db_session, bullet.id, "Did an even better thing")
    assert updated.content == "Did an even better thing"
    assert updated.keywords is None


def test_add_bullet_rejects_blank_content(db_session):
    company = add_company(db_session, "Acme Corp")
    position = add_position(db_session, company.id, position_title="Engineer")
    with pytest.raises(ValueError):
        add_bullet(db_session, position.id, "   ")


def test_delete_bullet_removes_it(db_session):
    company = add_company(db_session, "Acme Corp")
    position = add_position(db_session, company.id, position_title="Engineer")
    bullet = add_bullet(db_session, position.id, "Did a great thing")

    assert delete_bullet(db_session, bullet.id) is True
    assert db_session.get(ExperienceBullet, bullet.id) is None


def test_delete_bullet_missing_returns_false(db_session):
    assert delete_bullet(db_session, 9999) is False


def test_delete_position_cascades_to_bullets(db_session):
    company = add_company(db_session, "Acme Corp")
    position = add_position(db_session, company.id, position_title="Engineer")
    bullet = add_bullet(db_session, position.id, "Did a great thing")

    assert delete_position(db_session, position.id) is True
    assert db_session.get(ExperienceEntry, position.id) is None
    assert db_session.get(ExperienceBullet, bullet.id) is None


def test_delete_company_cascades_to_positions_and_bullets(db_session):
    company = add_company(db_session, "Acme Corp")
    position = add_position(db_session, company.id, position_title="Engineer")
    bullet = add_bullet(db_session, position.id, "Did a great thing")

    assert delete_company(db_session, company.id) is True
    assert db_session.get(ExperienceEntry, company.id) is None
    assert db_session.get(ExperienceEntry, position.id) is None
    assert db_session.get(ExperienceBullet, bullet.id) is None


def test_delete_company_missing_returns_false(db_session):
    assert delete_company(db_session, 9999) is False
