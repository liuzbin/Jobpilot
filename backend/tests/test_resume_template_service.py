"""MD 简历模板库：CRUD + 默认模板唯一不变量 + 渲染校验。"""

from __future__ import annotations

import pytest

from app.models.tables import ResumeTemplate
from app.services.resume_template_service import (
    DEFAULT_TEMPLATE_MARKDOWN,
    ResumeTemplateNotFoundError,
    ResumeTemplateRenderError,
    create_template,
    delete_template,
    get_or_create_default_template,
    get_template,
    list_templates,
    render_template_markdown,
    set_default_template,
    update_template_content,
)

MINIMAL_CONTEXT = {
    "basic": {"full_name": "Jane Doe"},
    "summary": [],
    "skills": [],
    "experience": [],
    "projects": [],
    "education": [],
}


def test_get_or_create_default_template_seeds_builtin_default(db_session):
    assert db_session.query(ResumeTemplate).count() == 0
    template = get_or_create_default_template(db_session)
    assert template.is_default is True
    assert template.content == DEFAULT_TEMPLATE_MARKDOWN
    # 幂等：再调一次不会重复创建
    again = get_or_create_default_template(db_session)
    assert again.id == template.id
    assert db_session.query(ResumeTemplate).count() == 1


def test_create_template_first_one_becomes_default_automatically(db_session):
    template = create_template(db_session, "我的模板", "# {{ basic.full_name }}")
    assert template.is_default is True


def test_create_second_template_does_not_touch_default_unless_asked(db_session):
    first = create_template(db_session, "模板一", "# {{ basic.full_name }}")
    second = create_template(db_session, "模板二", "## {{ basic.full_name }}")
    assert first.is_default is True
    assert second.is_default is False


def test_create_template_with_set_default_switches_default(db_session):
    first = create_template(db_session, "模板一", "# {{ basic.full_name }}")
    second = create_template(db_session, "模板二", "## {{ basic.full_name }}", set_default=True)
    db_session.refresh(first)
    assert second.is_default is True
    assert first.is_default is False


def test_set_default_template_enforces_single_default(db_session):
    first = create_template(db_session, "模板一", "# {{ basic.full_name }}")
    second = create_template(db_session, "模板二", "## {{ basic.full_name }}")
    set_default_template(db_session, second.id)
    db_session.refresh(first)
    db_session.refresh(second)
    assert first.is_default is False
    assert second.is_default is True


def test_create_template_rejects_broken_jinja_syntax(db_session):
    with pytest.raises(ResumeTemplateRenderError):
        create_template(db_session, "坏模板", "{% for x in %}")


def test_create_template_rejects_empty_name_or_content(db_session):
    with pytest.raises(ValueError):
        create_template(db_session, "", "content")
    with pytest.raises(ValueError):
        create_template(db_session, "name", "   ")


def test_update_template_content_revalidates_rendering(db_session):
    template = create_template(db_session, "模板", "# {{ basic.full_name }}")
    with pytest.raises(ResumeTemplateRenderError):
        update_template_content(db_session, template.id, content="{% for x in %}")
    # 校验失败不应该污染已经存好的内容
    db_session.refresh(template)
    assert template.content == "# {{ basic.full_name }}"

    updated = update_template_content(db_session, template.id, name="新名字", content="## {{ basic.full_name }}")
    assert updated.name == "新名字"
    assert updated.content == "## {{ basic.full_name }}"


def test_delete_template_promotes_another_when_default_is_removed(db_session):
    first = create_template(db_session, "模板一", "# {{ basic.full_name }}")
    second = create_template(db_session, "模板二", "## {{ basic.full_name }}")
    assert first.is_default is True

    delete_template(db_session, first.id)
    db_session.refresh(second)
    assert second.is_default is True
    assert list_templates(db_session) == [second]


def test_delete_template_refuses_to_remove_the_last_one(db_session):
    template = create_template(db_session, "唯一模板", "# {{ basic.full_name }}")
    with pytest.raises(ValueError):
        delete_template(db_session, template.id)
    assert db_session.query(ResumeTemplate).count() == 1


def test_get_template_missing_raises_not_found(db_session):
    with pytest.raises(ResumeTemplateNotFoundError):
        get_template(db_session, 9999)


def test_render_template_markdown_missing_optional_field_renders_blank():
    """占位符对应的字段没提供时应该渲染成空字符串，不应该抛异常——模板
    作者不需要为每个字段都做"存不存在"的判断。"""
    output = render_template_markdown("Hello {{ basic.full_name }} {{ basic.nonexistent_field }}!", MINIMAL_CONTEXT)
    assert output == "Hello Jane Doe !"


def test_render_template_markdown_syntax_error_raises_friendly_error():
    with pytest.raises(ResumeTemplateRenderError):
        render_template_markdown("{% for x in %}", MINIMAL_CONTEXT)


def test_default_template_markdown_renders_all_sections():
    """默认模板本身要能正常渲染出完整的六个板块，回归防止以后有人手滑
    改坏了内置模板的 Jinja2 语法或者字段名。"""
    context = {
        "basic": {
            "full_name": "Jane Doe",
            "current_location": "Toronto, ON",
            "phone": "+1 000",
            "email": "jane@example.com",
            "github_url": "https://github.com/jane",
        },
        "summary": ["Summary line"],
        "skills": ["Skill line"],
        "experience": [
            {
                "company_name": "Acme",
                "position_title": "Engineer",
                "project_name": "Engineer",
                "date_range": "2020 – 至今",
                "bullet_items": [{"action_summary": "Did stuff", "result_summary": "50% faster"}],
            }
        ],
        "projects": [{"project_name": "Side Project", "date_range": "2022", "bullets": ["Built a thing"]}],
        "education": [{"school": "State U", "degree": "BSc", "location": "City", "date_range": "2016 – 2020"}],
    }
    output = render_template_markdown(DEFAULT_TEMPLATE_MARKDOWN, context)
    assert "Jane Doe" in output
    assert "Summary line" in output
    assert "Skill line" in output
    assert "**Acme** | *Engineer*" in output
    assert "Did stuff，50% faster" in output
    assert "**Side Project**" in output
    assert "**State U** | City" in output
