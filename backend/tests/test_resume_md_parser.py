"""打磨阶段用户反馈第 2 点：MD 简历模板的规则解析（不走 LLM）。"""

from __future__ import annotations

from app.services.resume_md_parser import parse_resume_markdown

SAMPLE_RESUME_MD = """\
<div align="center">
  <h1>Jane Example Doe</h1>
  <p>Toronto, ON | +1 647 000-0000 | <a href="mailto:jane@example.com">jane@example.com</a> | <a href="https://github.com/janedoe">https://github.com/janedoe</a></p>
</div>

## PROFESSIONAL SUMMARY

- **3+ years** of backend engineering experience.
- Strong track record shipping distributed systems.

## TECHNICAL SKILLS

- **Databases:** PostgreSQL, MySQL.
- **Languages:** Python, Go.

## PROFESSIONAL EXPERIENCE

**Acme Corp** | *Senior Engineer*
<div align="right"><i>Feb 2022 – July 2024</i></div>

- Led migration of the billing pipeline.
- Reduced latency by 30%.

**Acme Corp** | *Engineer*
<div align="right"><i>2020 – 2022</i></div>

- Built the initial ETL pipeline.

## PROJECT EXPERIENCE

**Side Project Bot**
<div align="right"><i>2023 – 至今</i></div>

- Built a Slack bot for standups.

## EDUCATION

**Example University** | Example City
*Bachelor of Science in Computer Science*
<div align="right"><i>Sep 2016 – Jun 2020</i></div>
"""

FREEFORM_RESUME_TEXT = """\
Jane Doe
Software Engineer with 5 years of experience.

Worked at various companies building things.
"""


def test_parses_full_sample_end_to_end():
    result = parse_resume_markdown(SAMPLE_RESUME_MD)
    assert result is not None

    basic = result["basic"]
    assert basic["full_name"] == "Jane Example Doe"
    assert basic["email"] == "jane@example.com"
    assert basic["phone"] == "+1 647 000-0000"
    assert basic["github_url"] == "https://github.com/janedoe"
    assert basic["current_location"] == "Toronto, ON"
    assert "3+ years" in basic["resume_summary"]
    assert "Databases: PostgreSQL, MySQL." in basic["skills_text"]
    # 教育经历第一条顺带回填旧的单值字段，和 LLM 抽取路径保持兼容
    assert basic["school"] == "Example University"
    assert basic["education"] == "Bachelor of Science in Computer Science"

    assert len(result["companies"]) == 1
    company = result["companies"][0]
    assert company["company_name"] == "Acme Corp"
    assert len(company["positions"]) == 2
    senior = company["positions"][0]
    assert senior["position_title"] == "Senior Engineer"
    assert senior["project_name"] == "Senior Engineer"  # 没有单独的项目名，复用岗位名称
    assert senior["start_date"] == "2022-02"
    assert senior["end_date"] == "2024-07"
    assert senior["is_current"] is False
    assert "Led migration of the billing pipeline." in senior["bullets"]

    assert len(result["projects"]) == 1
    project = result["projects"][0]
    assert project["project_name"] == "Side Project Bot"
    assert project["start_date"] == "2023"
    assert project["is_current"] is True
    assert project["end_date"] is None

    assert len(result["education_entries"]) == 1
    edu = result["education_entries"][0]
    assert edu["school"] == "Example University"
    assert edu["location"] == "Example City"
    assert edu["degree"] == "Bachelor of Science in Computer Science"
    assert edu["start_date"] == "2016-09"
    assert edu["end_date"] == "2020-06"


def test_returns_none_for_freeform_text_without_known_sections():
    """随手写的自由格式简历（没有任何一个认识的章节标题）应该老老实实
    返回 None，让调用方回退到 LLM 抽取，而不是勉强凑一个残缺结果。"""
    assert parse_resume_markdown(FREEFORM_RESUME_TEXT) is None


def test_returns_none_for_empty_text():
    assert parse_resume_markdown("") is None
    assert parse_resume_markdown("   \n  ") is None


def test_recognizes_alternate_section_header_wording():
    """标题措辞稍有不同（比如 "Work Experience" 而不是 "Professional
    Experience"）也应该能识别，不要求逐字匹配。"""
    text = """\
# John Smith

## Work Experience

**Beta LLC** | *Developer*
<div align="right"><i>2021 – 2023</i></div>

- Did some work.
"""
    result = parse_resume_markdown(text)
    assert result is not None
    assert result["basic"]["full_name"] == "John Smith"
    assert result["companies"][0]["company_name"] == "Beta LLC"


def test_missing_date_or_malformed_entry_does_not_crash():
    """日期格式解析不出来时不应该抛异常，缺失的一侧留 None。"""
    text = """\
# Someone

## Professional Experience

**Some Co** | *Role*
<div align="right"><i>not-a-real-date</i></div>

- Did something.
"""
    result = parse_resume_markdown(text)
    assert result is not None
    position = result["companies"][0]["positions"][0]
    assert position["start_date"] is None
    assert position["end_date"] is None
    assert position["bullets"] == ["Did something."]
