"""JD 的入库与结构化解析。"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.core.llm_client import LLMClient
from app.models.tables import JDRecord, JDStatus

JD_EXTRACTION_SYSTEM_PROMPT = """\
你是一个招聘 JD 信息抽取助手。你会收到一段职位描述（JD）原文，需要抽取出结构化字段。

严格按下面的 JSON 结构输出，不要输出任何多余的解释文字：
{
  "required_years": 数字或null（JD明确要求的最低工作年限，没提到就是null）,
  "required_education": "JD要求的学历，比如 Bachelor's degree，没提到就是null",
  "required_clearance": true或false（JD是否要求特定的安全许可/清关身份/工签），
  "clearance_description": "清关/工签要求的具体描述，没有就是null",
  "plus_skills": ["JD中标注为 Plus/Preferred/Nice to have 的技能，逐条列出"],
  "core_responsibilities": ["核心职责，逐条列出，控制在5条以内"],
  "key_skills": ["JD中反复强调的核心技术关键词，逐条列出"]
}
"""

# 附加信息行（比如 LinkedIn 上的 "Toronto, ON · 2 weeks ago · 80 people clicked apply
# Promoted by hirer · Responses managed off LinkedIn"）本身格式比较固定，用规则解析，
# 不需要 LLM 参与，这样这部分信息的解析结果完全稳定。
_APPLICANTS_RE = re.compile(r"(\d+)\s*people\s*clicked\s*apply", re.IGNORECASE)
_POSTED_AGO_RE = re.compile(r"(\d+\s*(?:day|week|month|hour)s?\s*ago)", re.IGNORECASE)


def parse_extra_meta_rules(extra_meta_raw: str | None) -> dict:
    if not extra_meta_raw:
        return {}
    meta: dict = {}
    applicants_match = _APPLICANTS_RE.search(extra_meta_raw)
    if applicants_match:
        meta["applicants_clicked"] = int(applicants_match.group(1))
    posted_match = _POSTED_AGO_RE.search(extra_meta_raw)
    if posted_match:
        meta["posted_ago"] = posted_match.group(1)
    meta["is_promoted"] = "promoted" in extra_meta_raw.lower()
    meta["responses_managed_off_linkedin"] = "responses managed off linkedin" in extra_meta_raw.lower()
    return meta


def create_jd(
    db: Session,
    company: str | None,
    title: str | None,
    description_raw: str,
    location: str | None = None,
    source_url: str | None = None,
    extra_meta_raw: str | None = None,
) -> JDRecord:
    jd = JDRecord(
        company=company,
        title=title,
        description_raw=description_raw,
        location=location,
        source_url=source_url,
        extra_meta_raw=extra_meta_raw,
        status=JDStatus.PENDING,
        parsed_meta=parse_extra_meta_rules(extra_meta_raw) or None,
    )
    db.add(jd)
    db.commit()
    db.refresh(jd)
    return jd


def structure_jd_text(jd: JDRecord, llm_client: LLMClient) -> dict:
    result = llm_client.complete_json(JD_EXTRACTION_SYSTEM_PROMPT, jd.description_raw)
    result.setdefault("required_years", None)
    result.setdefault("required_education", None)
    result.setdefault("required_clearance", False)
    result.setdefault("plus_skills", [])
    result.setdefault("core_responsibilities", [])
    result.setdefault("key_skills", [])
    return result
