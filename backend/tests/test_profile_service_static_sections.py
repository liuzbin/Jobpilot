"""打磨阶段后新增：教育经历 / 独立项目这两块"静态背景信息"的合并 + 手动
增删改，以及 format_date_range 展示格式化。个人总结/技能字段直接复用
BASIC_FIELDS 那套"只填空"逻辑，已经被 test_profile_service.py 里针对
BASIC_FIELDS 的既有测试覆盖，这里不重复。"""

from __future__ import annotations

import pytest

from app.models.tables import EducationEntry, PersonalProject, PersonalProjectBullet
from app.services.profile_service import (
    add_education_entry,
    add_personal_project,
    add_personal_project_bullet,
    delete_education_entry,
    delete_personal_project,
    delete_personal_project_bullet,
    format_date_range,
    get_education_entries,
    get_personal_projects,
    merge_parsed_experience,
    update_education_entry,
    update_personal_project,
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
