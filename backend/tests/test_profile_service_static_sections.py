"""打磨阶段后新增：教育经历 / 独立项目这两块"静态背景信息"的合并 + 手动
增删改，以及 format_date_range 展示格式化。个人总结/技能字段直接复用
BASIC_FIELDS 那套"只填空"逻辑，已经被 test_profile_service.py 里针对
BASIC_FIELDS 的既有测试覆盖，这里不重复。"""

from __future__ import annotations

import pytest

from app.models.tables import EducationEntry, PersonalProject, PersonalProjectBullet, ProfileSkill
from app.services.profile_service import (
    add_education_entry,
    add_personal_project,
    add_personal_project_bullet,
    add_profile_skill,
    delete_education_entry,
    delete_personal_project,
    delete_personal_project_bullet,
    delete_profile_skill,
    format_date_range,
    get_education_entries,
    get_or_create_profile_basic,
    get_personal_projects,
    get_profile_skills,
    merge_parsed_experience,
    update_education_entry,
    update_personal_project,
    update_profile_basic,
)


def _parsed_with(**kwargs):
    base = {"basic": {}, "companies": []}
    base.update(kwargs)
    return base


# ---------- format_date_range ----------


def test_format_date_range_with_month_and_year():
    assert format_date_range("2022-06", "2023-09", False) == "Jun 2022 – Sep 2023"


def test_format_date_range_ongoing():
    assert format_date_range("2022-06", None, True) == "Jun 2022 – 至今"


def test_format_date_range_year_only():
    assert format_date_range("2020", "2021", False) == "2020 – 2021"


def test_format_date_range_missing_end_and_not_current():
    assert format_date_range("2022-06", None, False) == "Jun 2022"


def test_format_date_range_all_missing_returns_empty_string():
    assert format_date_range(None, None, False) == ""


# ---------- 教育经历合并 ----------


def test_merge_education_entries_adds_new(db_session):
    parsed = _parsed_with(
        education_entries=[
            {"school": "MIT", "degree": "BSc", "location": "Cambridge", "start_date": "2016-09", "end_date": "2020-06"}
        ]
    )
    result = merge_parsed_experience(db_session, parsed)
    assert result.education_added == 1
    entries = get_education_entries(db_session)
    assert len(entries) == 1
    assert entries[0].school == "MIT"


def test_merge_education_entries_dedupes_exact_school_and_degree(db_session):
    parsed = _parsed_with(education_entries=[{"school": "MIT", "degree": "BSc", "start_date": "2016-09"}])
    merge_parsed_experience(db_session, parsed)
    result2 = merge_parsed_experience(db_session, parsed)
    assert result2.education_added == 0
    assert db_session.query(EducationEntry).count() == 1


def test_merge_education_entries_case_insensitive_dedup(db_session):
    merge_parsed_experience(db_session, _parsed_with(education_entries=[{"school": "MIT", "degree": "BSc"}]))
    result2 = merge_parsed_experience(db_session, _parsed_with(education_entries=[{"school": "mit", "degree": "bsc"}]))
    assert result2.education_added == 0


def test_merge_education_entries_different_degree_same_school_both_added(db_session):
    merge_parsed_experience(db_session, _parsed_with(education_entries=[{"school": "HEU", "degree": "Master"}]))
    result2 = merge_parsed_experience(db_session, _parsed_with(education_entries=[{"school": "HEU", "degree": "Bachelor"}]))
    assert result2.education_added == 1
    assert db_session.query(EducationEntry).count() == 2


# ---------- 教育经历手动增删改 ----------


def test_add_education_entry_requires_school(db_session):
    with pytest.raises(ValueError):
        add_education_entry(db_session, "")


def test_update_and_delete_education_entry(db_session):
    entry = add_education_entry(db_session, "MIT", degree="BSc")
    updated = update_education_entry(db_session, entry.id, {"degree": "MSc"})
    assert updated.degree == "MSc"
    assert delete_education_entry(db_session, entry.id) is True
    assert get_education_entries(db_session) == []


def test_delete_education_entry_missing_returns_false(db_session):
    assert delete_education_entry(db_session, 9999) is False


# ---------- 独立项目合并 ----------


def test_merge_personal_projects_adds_new_with_bullets(db_session):
    parsed = _parsed_with(
        projects=[
            {
                "project_name": "Side Bot",
                "start_date": "2023",
                "end_date": None,
                "is_current": True,
                "bullets": ["Built a thing", "Shipped it"],
            }
        ]
    )
    result = merge_parsed_experience(db_session, parsed)
    assert result.projects_added == 1
    assert result.project_bullets_added == 2
    projects = get_personal_projects(db_session)
    assert len(projects) == 1
    assert {b.content for b in projects[0].bullets} == {"Built a thing", "Shipped it"}


def test_merge_personal_projects_exact_name_match_merges_bullets(db_session):
    merge_parsed_experience(db_session, _parsed_with(projects=[{"project_name": "Side Bot", "bullets": ["First bullet"]}]))
    result2 = merge_parsed_experience(
        db_session, _parsed_with(projects=[{"project_name": "side bot", "bullets": ["First bullet", "Second bullet"]}])
    )
    assert result2.projects_added == 0  # 名称标准化后完全一致，复用已有项目
    assert result2.project_bullets_added == 1  # 只有新的那条贡献句被加进去
    assert db_session.query(PersonalProject).count() == 1


def test_merge_personal_projects_different_names_stay_separate(db_session):
    merge_parsed_experience(db_session, _parsed_with(projects=[{"project_name": "Bot One"}]))
    result2 = merge_parsed_experience(db_session, _parsed_with(projects=[{"project_name": "Bot Two"}]))
    assert result2.projects_added == 1
    assert db_session.query(PersonalProject).count() == 2


# ---------- 独立项目手动增删改 ----------


def test_add_personal_project_requires_name(db_session):
    with pytest.raises(ValueError):
        add_personal_project(db_session, "")


def test_update_and_delete_personal_project(db_session):
    project = add_personal_project(db_session, "My Project", start_date="2022-01")
    updated = update_personal_project(db_session, project.id, {"project_name": "Renamed Project"})
    assert updated.project_name == "Renamed Project"
    assert delete_personal_project(db_session, project.id) is True
    assert get_personal_projects(db_session) == []


def test_add_and_delete_personal_project_bullet(db_session):
    project = add_personal_project(db_session, "My Project")
    bullet = add_personal_project_bullet(db_session, project.id, "Did a thing")
    assert bullet.content == "Did a thing"
    assert delete_personal_project_bullet(db_session, bullet.id) is True
    assert db_session.query(PersonalProjectBullet).count() == 0


def test_add_personal_project_bullet_requires_existing_project(db_session):
    with pytest.raises(ValueError):
        add_personal_project_bullet(db_session, 9999, "content")


def test_add_personal_project_bullet_requires_content(db_session):
    project = add_personal_project(db_session, "My Project")
    with pytest.raises(ValueError):
        add_personal_project_bullet(db_session, project.id, "   ")


# ---------- 独立项目关联公司标签（LinkedIn 画像功能新增） ----------


def test_merge_personal_projects_sets_company_tag_on_create(db_session):
    result = merge_parsed_experience(
        db_session, _parsed_with(projects=[{"project_name": "Side Bot", "company_tag": "Acme Corp"}])
    )
    assert result.projects_added == 1
    project = get_personal_projects(db_session)[0]
    assert project.company_tag == "Acme Corp"


def test_merge_personal_projects_fills_blank_tag_but_does_not_overwrite_existing(db_session):
    project = add_personal_project(db_session, "Side Bot")
    update_personal_project(db_session, project.id, {"company_tag": "Manually Set Co"})

    merge_parsed_experience(
        db_session, _parsed_with(projects=[{"project_name": "Side Bot", "company_tag": "From LinkedIn Co"}])
    )
    assert get_personal_projects(db_session)[0].company_tag == "Manually Set Co"

    # 一个还没打过标签的项目，合并进来的标签应该能正常填上去。
    other = add_personal_project(db_session, "Other Bot")
    merge_parsed_experience(
        db_session, _parsed_with(projects=[{"project_name": "Other Bot", "company_tag": "From LinkedIn Co"}])
    )
    db_session.refresh(other)
    assert other.company_tag == "From LinkedIn Co"


def test_update_personal_project_tag_only_does_not_touch_other_fields(db_session):
    project = add_personal_project(db_session, "My Project", start_date="2022-01")
    updated = update_personal_project(db_session, project.id, {"company_tag": "Acme Corp"})
    assert updated.company_tag == "Acme Corp"
    assert updated.project_name == "My Project"
    assert updated.start_date == "2022-01"


# ---------- 结构化技能标签合并 + 手动增删（LinkedIn 画像功能新增） ----------


def test_merge_skills_adds_new_and_dedupes_case_insensitively(db_session):
    result = merge_parsed_experience(db_session, _parsed_with(skills=["Python", "PostgreSQL", "python"]))
    assert result.skills_added == 2
    assert {s.skill_name for s in get_profile_skills(db_session)} == {"Python", "PostgreSQL"}

    result2 = merge_parsed_experience(db_session, _parsed_with(skills=["POSTGRESQL", "Go"]))
    assert result2.skills_added == 1
    assert {s.skill_name for s in get_profile_skills(db_session)} == {"Python", "PostgreSQL", "Go"}


def test_merge_skills_noop_for_resume_upload_parsed_dict_without_skills_key(db_session):
    # 简历上传解析出的 parsed 字典不带 "skills" 这个 key（技能走的是
    # skills_text 那套 BASIC_FIELDS 逻辑），确认这里不会因为 key 缺失报错，
    # 也确实不会新增任何 profile_skill 记录。
    result = merge_parsed_experience(db_session, _parsed_with())
    assert result.skills_added == 0
    assert get_profile_skills(db_session) == []


def test_add_profile_skill_requires_name(db_session):
    with pytest.raises(ValueError):
        add_profile_skill(db_session, "  ")


def test_add_profile_skill_rejects_duplicate(db_session):
    add_profile_skill(db_session, "Python")
    with pytest.raises(ValueError):
        add_profile_skill(db_session, "python")


def test_delete_profile_skill(db_session):
    skill = add_profile_skill(db_session, "Python")
    assert delete_profile_skill(db_session, skill.id) is True
    assert get_profile_skills(db_session) == []


def test_delete_profile_skill_missing_returns_false(db_session):
    assert delete_profile_skill(db_session, 9999) is False


# ---------- additional_notes（LinkedIn 画像功能新增） ----------


def test_update_profile_basic_partial_dict_does_not_clobber_other_fields(db_session):
    update_profile_basic(db_session, {"full_name": "Alice", "target_title": "Engineer"})
    update_profile_basic(db_session, {"additional_notes": "Prefers remote work."})
    profile = get_or_create_profile_basic(db_session)
    assert profile.full_name == "Alice"
    assert profile.target_title == "Engineer"
    assert profile.additional_notes == "Prefers remote work."
