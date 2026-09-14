"""
打磨阶段用户反馈第 2 点的落地：简历如果是按 JobPilot 的 MD 模板格式上传的，
不需要 LLM 就能确定性地把内容解析成和 `resume_ingest.structure_resume_text`
（LLM 抽取）完全同构的结构化字典——两条路径产出的字典形状必须一致，因为
下游 `profile_service.merge_parsed_experience` 不关心数据是规则解析来的还是
LLM 抽取来的。

这里"能不能解析"的判定很直接：扫描 Markdown 里的二级标题（`## `），只要一个
都没有命中下面 `_SECTION_ALIASES` 认识的几种写法，就认为这不是我们自己的
模板格式（用户随手写的自由格式简历、或者别的来源导出的 md），直接返回
`None`，调用方（resume_ingest.extract_and_structure）据此回退到 LLM 抽取，
而不是勉强解析出一个残缺甚至错误的结构。

这不是一个通用的"任意 Markdown 简历解析器"——特意按 JobPilot 默认模板
（`resume_template_service.DEFAULT_TEMPLATE_MARKDOWN` 去掉 Jinja2 占位符
之后的样子）的具体排版约定来写规则，宽松兼容几种常见的标题措辞和日期写法，
但不追求识别所有可能的 Markdown 简历排版——那是 LLM 兜底该做的事。
"""

from __future__ import annotations

import re

_TAG_RE = re.compile(r"<[^>]+>")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_ONLY_RE = re.compile(r"(?<!\*)\*([^*]+?)\*(?!\*)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_MAILTO_HREF_RE = re.compile(r'href="mailto:([^"]+)"', re.IGNORECASE)
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{7,}\d")
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>|^#\s+(.+)$", re.IGNORECASE | re.DOTALL | re.MULTILINE)
_P_BLOCK_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_SECTION_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_DATE_DIV_RE = re.compile(r"<div[^>]*align=[\"']right[\"'][^>]*>\s*<i>(.*?)</i>\s*</div>", re.IGNORECASE | re.DOTALL)
_BULLET_LINE_RE = re.compile(r"^\s*-\s+(.+?)\s*$")
_COMPANY_TITLE_LINE_RE = re.compile(r"^\*\*(.+?)\*\*\s*\|\s*\*(.+?)\*\s*$")
_BOLD_ONLY_LINE_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")
_SCHOOL_LOCATION_LINE_RE = re.compile(r"^\*\*(.+?)\*\*\s*\|\s*(.+?)\s*$")
_DEGREE_LINE_RE = re.compile(r"^\*([^*]+?)\*\s*$")

_ONGOING_WORDS = {"至今", "present", "current", "now", "ongoing"}

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MONTH_YEAR_RE = re.compile(r"^([A-Za-z]+)\s+(\d{4})$")
_YEAR_ONLY_RE = re.compile(r"^(\d{4})$")

_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "summary": ("professional summary", "summary", "个人总结", "个人简介"),
    "skills": ("technical skills", "skills", "技能", "技术技能"),
    "experience": ("professional experience", "work experience", "experience", "工作经历"),
    "projects": ("project experience", "projects", "项目经历"),
    "education": ("education", "教育背景", "教育经历"),
}


def _strip_tags_and_markup(text: str) -> str:
    text = _TAG_RE.sub("", text or "")
    text = _BOLD_RE.sub(r"\1", text)
    text = _ITALIC_ONLY_RE.sub(r"\1", text)
    return text.strip()


def _split_dash(text: str) -> list[str]:
    for dash in ("–", "—", " - "):
        if dash in text:
            return [p.strip() for p in text.split(dash, 1)]
    return [text.strip()]


def _parse_one_date(token: str) -> str | None:
    token = token.strip()
    if not token:
        return None
    match = _MONTH_YEAR_RE.match(token)
    if match:
        month_name, year = match.group(1).lower(), match.group(2)
        month = _MONTHS.get(month_name)
        if month:
            return f"{year}-{month:02d}"
        return year
    match = _YEAR_ONLY_RE.match(token)
    if match:
        return match.group(1)
    return None


def _parse_date_range(text: str) -> tuple[str | None, str | None, bool]:
    """解析形如 "Feb 2025 – July 2025" / "Sep 2019 – Mar 2022" / "2025 – 2026" /
    "2025 – 至今" 的日期区间文本，返回 (start_date, end_date, is_current)，
    统一成和 `_YEAR_MONTH_RE`（profile_service.py）兼容的 "YYYY-MM"/"YYYY" 格式。
    解析不出来的一侧留 None，不抛异常——日期格式偶尔对不上不应该让整段经历
    解析全部失败。"""
    parts = _split_dash(text)
    start_raw = parts[0] if parts else ""
    end_raw = parts[1] if len(parts) > 1 else ""

    start_date = _parse_one_date(start_raw)
    if end_raw.strip().lower() in _ONGOING_WORDS:
        return start_date, None, True
    end_date = _parse_one_date(end_raw)
    return start_date, end_date, False


def _extract_basic(header_block: str) -> dict:
    basic: dict = {
        "full_name": None,
        "email": None,
        "phone": None,
        "linkedin_url": None,
        "github_url": None,
        "current_location": None,
    }

    h1_match = _H1_RE.search(header_block)
    if h1_match:
        name_raw = h1_match.group(1) or h1_match.group(2) or ""
        basic["full_name"] = _strip_tags_and_markup(name_raw) or None

    p_match = _P_BLOCK_RE.search(header_block)
    contact_raw = p_match.group(1) if p_match else header_block

    mailto_match = _MAILTO_HREF_RE.search(contact_raw)
    if mailto_match:
        basic["email"] = mailto_match.group(1).strip()

    for href in _HREF_RE.findall(contact_raw):
        href_lower = href.lower()
        if "github.com" in href_lower:
            basic["github_url"] = href.strip()
        elif "linkedin.com" in href_lower:
            basic["linkedin_url"] = href.strip()

    contact_text = _strip_tags_and_markup(contact_raw)
    if basic["email"] is None:
        email_match = _EMAIL_RE.search(contact_text)
        if email_match:
            basic["email"] = email_match.group(0)

    phone_match = _PHONE_RE.search(contact_text)
    if phone_match:
        basic["phone"] = phone_match.group(0).strip()

    # 联系人信息行剩下的、既不是邮箱/电话/URL 的第一个 " | " 分段，当成
    # 所在地——模板约定第一段就是地点（"Toronto, ON | +1 ... | email | github"）。
    for segment in contact_text.split("|"):
        segment = segment.strip()
        if not segment:
            continue
        if segment == basic["email"] or segment == basic["phone"]:
            continue
        if segment.startswith("http") or "github.com" in segment.lower() or "linkedin.com" in segment.lower():
            continue
        basic["current_location"] = segment
        break

    return basic


def _find_sections(body: str) -> list[tuple[str, str]]:
    """按 `## ` 标题切分正文，返回 [(规范化后的 section key 或原始标题, 段落内容)]。
    识别不出来的标题原样保留 key（调用方会跳过），不会导致整体解析失败。"""
    headers = list(_SECTION_HEADER_RE.finditer(body))
    sections: list[tuple[str, str]] = []
    for i, match in enumerate(headers):
        title = match.group(1).strip()
        start = match.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        content = body[start:end]

        normalized = title.strip().lower()
        key = title
        for canonical, aliases in _SECTION_ALIASES.items():
            if normalized in aliases:
                key = canonical
                break
        sections.append((key, content))
    return sections


def _parse_bullets(block: str) -> list[str]:
    bullets = []
    for line in block.splitlines():
        match = _BULLET_LINE_RE.match(line)
        if match:
            text = _strip_tags_and_markup(match.group(1))
            if text:
                bullets.append(text)
    return bullets


def _parse_experience_section(content: str) -> list[dict]:
    """PROFESSIONAL EXPERIENCE：每条经历是 `**公司** | *岗位*` 起始，后面跟一行
    右对齐的日期 div，再往后是若干条贡献句，直到下一条经历或本节结束。
    `project_name` 模板里不单独出现，复用岗位名称——和
    `resume_ingest.RESUME_EXTRACTION_SYSTEM_PROMPT` 里"没有项目名就用岗位
    名称重复一次"的既有约定保持一致，两条路径产出的结构不因为解析方式不同
    而不一样。"""
    lines = content.splitlines()
    entries: list[dict] = []
    current: dict | None = None
    pending_lines: list[str] = []

    def _flush():
        if current is None:
            return
        block_text = "\n".join(pending_lines)
        date_match = _DATE_DIV_RE.search(block_text)
        start_date = end_date = None
        is_current = False
        if date_match:
            start_date, end_date, is_current = _parse_date_range(_strip_tags_and_markup(date_match.group(1)))
        current["start_date"] = start_date
        current["end_date"] = end_date
        current["is_current"] = is_current
        current["bullets"] = _parse_bullets(block_text)
        entries.append(current)

    for line in lines:
        header_match = _COMPANY_TITLE_LINE_RE.match(line.strip())
        if header_match:
            _flush()
            current = {
                "company_name": _strip_tags_and_markup(header_match.group(1)),
                "position_title": _strip_tags_and_markup(header_match.group(2)),
            }
            current["project_name"] = current["position_title"]
            pending_lines = []
            continue
        if current is not None:
            pending_lines.append(line)
    _flush()
    return [e for e in entries if e.get("company_name")]


def _parse_projects_section(content: str) -> list[dict]:
    """PROJECT EXPERIENCE：独立项目，每条只有一个 `**项目名**`（没有公司/岗位），
    后面同样是日期 div + 贡献句列表。"""
    lines = content.splitlines()
    entries: list[dict] = []
    current: dict | None = None
    pending_lines: list[str] = []

    def _flush():
        if current is None:
            return
        block_text = "\n".join(pending_lines)
        date_match = _DATE_DIV_RE.search(block_text)
        start_date = end_date = None
        is_current = False
        if date_match:
            start_date, end_date, is_current = _parse_date_range(_strip_tags_and_markup(date_match.group(1)))
        current["start_date"] = start_date
        current["end_date"] = end_date
        current["is_current"] = is_current
        current["bullets"] = _parse_bullets(block_text)
        entries.append(current)

    for line in lines:
        stripped = line.strip()
        # 项目条目的判定：整行就是一个粗体文本，且不是"公司 | 岗位"这种
        # 两段式（那属于工作经历），避免和 _parse_experience_section 的
        # 起始行判定搞混。
        if _COMPANY_TITLE_LINE_RE.match(stripped):
            continue
        header_match = _BOLD_ONLY_LINE_RE.match(stripped)
        if header_match:
            _flush()
            current = {"project_name": _strip_tags_and_markup(header_match.group(1))}
            pending_lines = []
            continue
        if current is not None:
            pending_lines.append(line)
    _flush()
    return [e for e in entries if e.get("project_name")]


def _parse_education_section(content: str) -> list[dict]:
    """EDUCATION：`**学校** | 地点` 一行，紧接着 `*学位*` 一行，再是日期 div。
    不含贡献句。"""
    lines = [ln for ln in content.splitlines()]
    entries: list[dict] = []
    current: dict | None = None
    pending_lines: list[str] = []

    def _flush():
        if current is None:
            return
        block_text = "\n".join(pending_lines)
        date_match = _DATE_DIV_RE.search(block_text)
        start_date = end_date = None
        is_current = False
        if date_match:
            start_date, end_date, is_current = _parse_date_range(_strip_tags_and_markup(date_match.group(1)))
        degree = None
        for line in pending_lines:
            degree_match = _DEGREE_LINE_RE.match(line.strip())
            if degree_match:
                degree = _strip_tags_and_markup(degree_match.group(1))
                break
        current["degree"] = degree
        current["start_date"] = start_date
        current["end_date"] = end_date
        current["is_current"] = is_current
        entries.append(current)

    for line in lines:
        stripped = line.strip()
        header_match = _SCHOOL_LOCATION_LINE_RE.match(stripped)
        # 学位那一行 `*学位*` 也可能被 _SCHOOL_LOCATION_LINE_RE 意外匹配到吗？
        # 不会——那个正则要求有 `**...**`（双星号）在前，学位行只有单星号。
        if header_match:
            _flush()
            current = {
                "school": _strip_tags_and_markup(header_match.group(1)),
                "location": _strip_tags_and_markup(header_match.group(2)),
            }
            pending_lines = []
            continue
        if current is not None:
            pending_lines.append(line)
    _flush()
    return [e for e in entries if e.get("school")]


def parse_resume_markdown(raw_text: str) -> dict | None:
    """尝试按 JobPilot MD 模板约定解析简历原文。识别不出任何一个已知章节标题
    时返回 None，调用方应该回退到 LLM 抽取（见 resume_ingest.py）。

    返回的字典结构和 `resume_ingest.structure_resume_text` 的 LLM 抽取结果
    同构，额外多了两个 key（education_entries/projects）——这两个字段两条
    路径都会产出（LLM 抽取的 prompt 里也定义了同样的字段），
    `profile_service.merge_parsed_experience` 统一处理，不区分数据来源。
    个人总结/技能这两块直接拼成多行文本放进 `basic.resume_summary`/
    `basic.skills_text`，复用 `profile_service.BASIC_FIELDS` 那套"只填空
    不覆盖"逻辑，不需要单独的合并函数。
    """
    if not raw_text or not raw_text.strip():
        return None

    first_header = _SECTION_HEADER_RE.search(raw_text)
    header_block = raw_text[: first_header.start()] if first_header else raw_text
    sections = _find_sections(raw_text) if first_header else []

    recognized_keys = {key for key, _ in sections if key in _SECTION_ALIASES}
    if not recognized_keys:
        return None

    basic = _extract_basic(header_block)
    summary_bullets: list[str] = []
    skills_bullets: list[str] = []
    education_entries: list[dict] = []
    companies_map: dict[str, dict] = {}
    projects: list[dict] = []

    for key, content in sections:
        if key == "summary":
            summary_bullets.extend(_parse_bullets(content))
        elif key == "skills":
            skills_bullets.extend(_parse_bullets(content))
        elif key == "experience":
            for entry in _parse_experience_section(content):
                company_name = entry["company_name"]
                company = companies_map.setdefault(
                    company_name.lower(), {"company_name": company_name, "positions": []}
                )
                company["positions"].append(
                    {
                        "position_title": entry.get("position_title"),
                        "project_name": entry.get("project_name"),
                        "start_date": entry.get("start_date"),
                        "end_date": entry.get("end_date"),
                        "is_current": entry.get("is_current", False),
                        "bullets": entry.get("bullets", []),
                    }
                )
        elif key == "projects":
            projects.extend(_parse_projects_section(content))
        elif key == "education":
            education_entries.extend(_parse_education_section(content))

    companies = list(companies_map.values())

    if not any([basic.get("full_name"), summary_bullets, skills_bullets, education_entries, companies, projects]):
        # 章节标题识别到了、但一条实质内容都没抽出来（比如格式和约定差太多），
        # 这种半吊子结果不如直接回退给 LLM。
        return None

    if education_entries:
        # 教育经历里的第一条（模板约定按时间倒序，最新的学历排最前）顺带填充
        # 旧的单值字段（profile_service.BASIC_FIELDS 里的 school/education），
        # 保持和 LLM 抽取路径的兼容——旧字段不会因为换了解析方式就再也填不上。
        basic["school"] = education_entries[0].get("school")
        basic["education"] = education_entries[0].get("degree")
    if summary_bullets:
        basic["resume_summary"] = "\n".join(summary_bullets)
    if skills_bullets:
        basic["skills_text"] = "\n".join(skills_bullets)

    return {
        "basic": basic,
        "education_entries": education_entries,
        "companies": companies,
        "projects": projects,
    }
