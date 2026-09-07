"""Phase 1：JD 入库 + 规则解析（parse_extra_meta_rules 是纯规则、不走 LLM 的部分，
解析结果应该完全稳定）。"""

from __future__ import annotations

from app.models.tables import JDStatus
from app.services.jd_ingest import create_jd, parse_extra_meta_rules


def test_parse_extra_meta_rules_extracts_all_fields():
    raw = "Toronto, ON · 2 weeks ago · 80 people clicked apply\nPromoted by hirer · Responses managed off LinkedIn"
    meta = parse_extra_meta_rules(raw)

    assert meta["applicants_clicked"] == 80
    assert meta["posted_ago"] == "2 weeks ago"
    assert meta["is_promoted"] is True
    assert meta["responses_managed_off_linkedin"] is True


def test_parse_extra_meta_rules_handles_missing_fields():
    meta = parse_extra_meta_rules("Toronto, ON · applications closed")

    assert "applicants_clicked" not in meta
    assert "posted_ago" not in meta
    assert meta["is_promoted"] is False
    assert meta["responses_managed_off_linkedin"] is False


def test_parse_extra_meta_rules_empty_input_returns_empty_dict():
    assert parse_extra_meta_rules(None) == {}
    assert parse_extra_meta_rules("") == {}


def test_parse_extra_meta_rules_is_case_insensitive():
    meta = parse_extra_meta_rules("PROMOTED · 5 DAYS AGO · 12 People Clicked Apply")
    assert meta["is_promoted"] is True
    assert meta["posted_ago"].lower() == "5 days ago"
    assert meta["applicants_clicked"] == 12


def test_parse_extra_meta_rules_is_deterministic():
    raw = "3 hours ago · 40 people clicked apply · Promoted by hirer"
    results = [parse_extra_meta_rules(raw) for _ in range(5)]
    assert all(r == results[0] for r in results)


def test_create_jd_stores_rule_parsed_meta_and_defaults_to_pending(db_session):
    jd = create_jd(
        db_session,
        company="Acme",
        title="Backend Engineer",
        description_raw="We need a backend engineer with 5 years experience.",
        location="Toronto, ON",
        source_url="https://example.com/job/1",
        extra_meta_raw="Toronto, ON · 1 week ago · 30 people clicked apply",
    )

    assert jd.id is not None
    assert jd.status == JDStatus.PENDING
    assert jd.parsed_meta["applicants_clicked"] == 30
    assert jd.parsed_meta["posted_ago"] == "1 week ago"


def test_create_jd_without_extra_meta_leaves_parsed_meta_none(db_session):
    jd = create_jd(db_session, company=None, title=None, description_raw="Just a JD body.")
    assert jd.parsed_meta is None
