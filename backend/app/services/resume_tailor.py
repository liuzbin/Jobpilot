"""
Phase 2：简历重制——见实施方案 4/5.3。核心是把 K 值语义落到"关键词 +
行为 + 结果"三元组这个颗粒度上：

- JD 要的关键词命中了画像里某条 bullet 的 keywords：直接用该 bullet 的真实
  三元组，只用 JD 的措辞习惯重组"行为"和"结果"的表达（任何 K 值下都会做，
  因为这一步不涉及编造，只是换个说法）。
- JD 要的关键词一条真实 bullet 都没命中：按 K 值决定要不要为其中一部分构造
  "建议三元组"——关键词是 JD 要的那个词，行为基于最相关项目的
  background_notes 推导，结果的量级参照用户其他真实 bullet 的历史区间校准。
  这一步全部先作为"待确认建议"返回，绝不直接写进最终简历；只有用户逐条确认
  过的，才会在 confirm_and_finalize 里真正生效，并被持久化进 claimed_skill
  供以后复用。

打分引擎（scoring.compute_score）完全不读这个模块产出的任何东西——这是刻意
的边界，见实施方案第一节"核心价值主张"。

无论是重组真实内容还是构造延伸建议，生成出来的自由文本都会先过一道事实字段
护栏校验（规则扫描疑似公司名 + 轻量模型语义比对），不通过就重试一次，仍不
通过就回退到真实原文（命中项）或整条丢弃（延伸建议），绝不把编造内容交给
用户确认——这一段是 v1 就定好、任何 K 值下都不放松的设计，见实施方案 5.3。

`confirm_and_finalize` 产出结构化 JSON + Markdown 预览之后，还会调用
`app.services.resume_pdf` 按 `style_id` 渲染一份 PDF 落盘（见 5.3 的"生成
管线"）；PDF 渲染失败不影响 JSON/Markdown 的可用性，只是当次没有 PDF 下载。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.llm_client import LLMClient
from app.models.tables import (
    ClaimedSkill,
    ExperienceEntry,
    ExperienceLevel,
    JDRecord,
    ResumeVersion,
)
from app.services.jd_ingest import structure_jd_text
from app.services.profile_service import (
    format_date_range,
    get_or_create_profile_basic,
    get_personal_projects,
)
from app.services.profile_service import get_education_entries as _get_education_entries
from app.services.resume_pdf import (
    AVAILABLE_RESUME_STYLES,
    MD_TEMPLATE_STYLE_SENTINEL,
    UnknownResumeStyleError,
    save_markdown_resume_pdf,
    save_resume_pdf,
)
from app.services.resume_template_service import (
    ResumeTemplateNotFoundError,
    get_template as get_resume_template,
    render_template_markdown,
)

logger = logging.getLogger("jobpilot")


class JDNotFoundError(RuntimeError):
    pass


class ResumeVersionNotFoundError(RuntimeError):
    pass


# ---------- 事实字段护栏校验（见实施方案 5.3：v1 就定好、任何 K 值下都不放松） ----------
#
# 结构上，公司名/职位名/项目名这些"事实性硬字段"从来不经过 LLM——它们始终是
# build_resume_draft/confirm_and_finalize 直接从数据库读出来拼进 resume_json 的,
# LLM 只被允许改写 action_summary/result_summary 这两段自由文本。但自由文本本身
# 仍然可能"顺嘴"编出一个不属于当前经历的公司名（比如把别的项目背景串进来,或者
# 干脆凭空提一个不存在的公司）,所以这里单独加一道自动校验：规则层先做一次
# 确定性的公司名扫描,轻量模型再做一次语义层面的事实一致性比对。任何一层校验
# 不通过就判定这条内容不可信,调用方负责重试或回退,绝不会把校验不通过的内容
# 直接交给用户。

_COMPANY_SUFFIX_PATTERN = re.compile(
    r"[一-龥A-Za-z0-9&()（）\.\-]{2,24}"
    r"(?:科技|集团|网络|信息技术|软件|工作室|"
    r"有限责任公司|股份有限公司|有限公司|公司|"
    r"Inc\.?|LLC|LLP|Corp\.?|Corporation|Ltd\.?|Co\.,?\s*Ltd\.?)"
)


def _known_companies(positions: list[ExperienceEntry]) -> set[str]:
    return {p.parent.company_name for p in positions if p.parent and p.parent.company_name}


def _rule_scan_foreign_companies(text: str, known_companies: set[str]) -> list[str]:
    """规则层：扫描文本里长得像公司名（常见公司后缀）的片段，任何一个都不是
    画像里真实存在的公司名时，判定为疑似编造。纯字符串规则，不依赖 LLM，
    结果确定性可复现，方便直接写单元测试锁定。"""
    violations = []
    for match in _COMPANY_SUFFIX_PATTERN.finditer(text or ""):
        candidate = match.group(0).strip()
        if not candidate:
            continue
        if any(candidate in company or company in candidate for company in known_companies if company):
            continue
        violations.append(f'文本中出现疑似公司名"{candidate}"，不在画像的真实公司列表中')
    return violations


FACT_GUARDRAIL_SYSTEM_PROMPT = """\
你负责批量校验若干段简历内容是否编造了和候选人真实背景不一致的具体公司、
职位、任职时间段或学历/学位信息。你会收到一组条目，每条包含它"应该"归属的
真实背景（候选人在哪家公司的一段经历），以及生成出来的文本。

对每一条：只有当文本明确提到了一个和真实背景不一致的具体公司名、职位名、
时间段或学历时才判定为不一致；纯粹的技术细节、措辞调整、没有点名任何具体
机构/时间/学历的表述都不算不一致。

严格按下面的 JSON 结构输出，`results` 数组长度和顺序必须和输入条目一一对应：
{"results": [{"consistent": true 或 false, "issues": ["..."]}]}
"""


def _batch_validate_facts(
    items: list[dict],
    true_companies: list[str | None],
    known_companies: set[str],
    light_client: LLMClient,
) -> list[list[str]]:
    """批量校验一组条目，返回每条对应的违规原因列表（空列表代表通过）。规则层
    对每条都单独跑（纯字符串扫描，零成本），轻量模型层为了控制调用次数、
    也为了和代码库里其它批量抽取（比如 extract_bullet_triads）保持同样的
    "一批一次调用"的约定，只发一次请求覆盖整批。"""
    if not items:
        return []

    texts = [f"{it.get('action_summary') or ''} {it.get('result_summary') or ''}".strip() for it in items]
    rule_violations = [_rule_scan_foreign_companies(t, known_companies) for t in texts]

    if not any(texts):
        # 整批都是空文本，没什么可校验的，不必浪费一次模型调用。
        return rule_violations

    entries_text = "\n".join(
        f"{i + 1}. 真实背景：候选人在「{company or '未知公司'}」的一段经历\n   生成文本：{text}"
        for i, (text, company) in enumerate(zip(texts, true_companies))
    )
    result = light_client.complete_json(FACT_GUARDRAIL_SYSTEM_PROMPT, entries_text)
    raw_results = result.get("results") or []

    combined: list[list[str]] = []
    for i in range(len(items)):
        violations = list(rule_violations[i])
        model_result = raw_results[i] if i < len(raw_results) and isinstance(raw_results[i], dict) else {}
        if not model_result.get("consistent", True):
            for issue in model_result.get("issues") or []:
                if issue and issue not in violations:
                    violations.append(issue)
        combined.append(violations)
    return combined


def validate_item_facts(
    item: dict,
    true_company: str | None,
    known_companies: set[str],
    light_client: LLMClient,
) -> list[str]:
    """单条校验，内部就是批量校验函数的单元素特例，主要给重试路径用（重试
    只需要重新校验刚重新生成的这一条，没必要凑一整批）。"""
    return _batch_validate_facts([item], [true_company], known_companies, light_client)[0]


def _validate_and_repair_hit_items(
    original_hits: list[dict],
    rewritten_items: list[dict],
    jd: JDRecord,
    known_companies: set[str],
    light_client: LLMClient,
    heavy_client: LLMClient,
) -> list[dict]:
    """对重写后的命中项批量跑护栏校验：不通过的逐条重试一次，还不通过就回退
    到未经改写的真实原文——原文本来就是从数据库里如实取出来的，必然通过
    校验，保证无论如何都不会把编造内容交给用户。"""
    true_companies = [item.get("company_name") for item in rewritten_items]
    all_violations = _batch_validate_facts(rewritten_items, true_companies, known_companies, light_client)

    repaired: list[dict] = []
    for original, item, violations in zip(original_hits, rewritten_items, all_violations):
        if not violations:
            repaired.append(item)
            continue

        retried_list = rewrite_hit_bullets([original], jd, heavy_client)
        retried = retried_list[0] if retried_list else dict(original)
        violations2 = validate_item_facts(retried, retried.get("company_name"), known_companies, light_client)
        repaired.append(retried if not violations2 else dict(original))
    return repaired


def _validate_and_filter_suggestions(
    suggestions: list[dict],
    positions: list[ExperienceEntry],
    known_companies: set[str],
    light_client: LLMClient,
    heavy_client: LLMClient,
) -> list[dict]:
    """对延伸建议批量跑护栏校验：不通过的针对该关键词重新生成一次，还不通过
    就整条丢弃——延伸建议本身就是构造出来的内容，没有"真实原文"可以回退，
    校验不过就不应该出现在待确认列表里。"""
    position_by_id = {p.id: p for p in positions}

    def _true_company(suggestion: dict) -> str | None:
        position = position_by_id.get(suggestion.get("experience_entry_id"))
        return position.parent.company_name if position and position.parent else None

    true_companies = [_true_company(s) for s in suggestions]
    all_violations = _batch_validate_facts(suggestions, true_companies, known_companies, light_client)

    valid: list[dict] = []
    for suggestion, violations in zip(suggestions, all_violations):
        if not violations:
            valid.append(suggestion)
            continue

        retried = generate_extension_suggestions(positions, [suggestion["keyword"]], heavy_client)
        retried_match = next(
            (s for s in retried if _norm_keyword(s.get("keyword")) == _norm_keyword(suggestion.get("keyword"))),
            None,
        )
        if retried_match is None:
            continue
        violations2 = validate_item_facts(retried_match, _true_company(retried_match), known_companies, light_client)
        if not violations2:
            valid.append(retried_match)
        # 仍不通过：整条丢弃，不进入待确认列表
    return valid


# ---------- 关键词命中判定（纯函数，不涉及 LLM） ----------


def _norm_keyword(k: str) -> str:
    return (k or "").strip().lower()


def collect_jd_keywords(jd_parsed: dict) -> list[str]:
    """JD 结构化解析结果里，"核心技能关键词"和"加分技能"都算作简历重制要
    覆盖的目标关键词，按 JD 里出现的顺序去重合并（key_skills 在前，视为
    优先级更高）。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for kw in [*(jd_parsed.get("key_skills") or []), *(jd_parsed.get("plus_skills") or [])]:
        kw = (kw or "").strip()
        norm = _norm_keyword(kw)
        if kw and norm not in seen:
            seen.add(norm)
            ordered.append(kw)
    return ordered


def find_missing_keywords(jd_keywords: list[str], positions: list[ExperienceEntry]) -> list[str]:
    """JD 目标关键词里，画像所有 bullet 的 keywords 字段都没命中的那些。"""
    covered: set[str] = set()
    for position in positions:
        for bullet in position.bullets:
            for kw in bullet.keywords or []:
                covered.add(_norm_keyword(kw))
    return [kw for kw in jd_keywords if _norm_keyword(kw) not in covered]


def find_hit_bullets(jd_keywords: list[str], positions: list[ExperienceEntry]) -> list[dict]:
    """JD 目标关键词命中的真实 bullet，返回三元组连同关联的关键词、所属 B 层信息。"""
    jd_keyword_set = {_norm_keyword(kw) for kw in jd_keywords}
    hits: list[dict] = []
    for position in positions:
        for bullet in position.bullets:
            matched = [kw for kw in (bullet.keywords or []) if _norm_keyword(kw) in jd_keyword_set]
            if not matched:
                continue
            hits.append(
                {
                    "experience_entry_id": position.id,
                    "bullet_id": bullet.id,
                    "company_name": position.parent.company_name if position.parent else None,
                    "position_title": position.position_title,
                    "project_name": position.project_name,
                    "matched_keywords": matched,
                    "keywords": bullet.keywords or [],
                    "action_summary": bullet.action_summary or bullet.content,
                    "result_summary": bullet.result_summary,
                    "original_content": bullet.content,
                }
            )
    return hits


def select_keywords_to_extend(missing_keywords: list[str], k_value: int) -> list[str]:
    """K=0 完全不做延伸；K=1..10 按比例从缺失关键词里选出一部分去尝试构造
    延伸建议，K 越大数量越多，K=10 覆盖全部。纯函数，方便单测锁定这条
    "K 值决定延伸幅度"的核心规则，不依赖 LLM，同样输入必须每次结果一致。
    """
    if k_value <= 0 or not missing_keywords:
        return []
    k_value = min(10, max(0, k_value))
    count = math.ceil(len(missing_keywords) * k_value / 10)
    count = min(count, len(missing_keywords))
    return missing_keywords[:count]


# ---------- 命中 bullet 的措辞重组（真实内容，仅调整表达） ----------

REWRITE_HITS_SYSTEM_PROMPT = """\
你会收到候选人若干条真实的工作贡献（关键词/行为/结果三元组）,以及目标 JD 的
核心职责描述。请只调整"行为"和"结果"的措辞和表达方式，让它们更贴合 JD 的
用词习惯，不改变任何事实内容——不能新增、删除或替换任何关键词，不能改变
结果的数值或量级，只是换一种更贴合 JD 语境的说法。

严格按下面的 JSON 结构输出，数组长度和顺序必须和输入的贡献列表一一对应：
{
  "rewritten": [
    {"action_summary": "...", "result_summary": "...或 null"}
  ]
}
"""


def rewrite_hit_bullets(hits: list[dict], jd: JDRecord, llm_client: LLMClient) -> list[dict]:
    if not hits:
        return []
    items_text = "\n".join(
        f"{i + 1}. 关键词: {', '.join(h['matched_keywords'])} | 行为: {h['action_summary']} | "
        f"结果: {h['result_summary'] or '（无）'}"
        for i, h in enumerate(hits)
    )
    user_prompt = f"目标 JD 核心职责/关键词：\n{jd.description_raw[:2000]}\n\n候选人真实贡献：\n{items_text}"
    result = llm_client.complete_json(REWRITE_HITS_SYSTEM_PROMPT, user_prompt)
    rewritten = result.get("rewritten") or []
    out = []
    for i, hit in enumerate(hits):
        item = rewritten[i] if i < len(rewritten) and isinstance(rewritten[i], dict) else {}
        out.append(
            {
                **hit,
                "action_summary": item.get("action_summary") or hit["action_summary"],
                "result_summary": item.get("result_summary") or hit["result_summary"],
            }
        )
    return out


# ---------- 技能延伸建议（待确认，绝不直接写入） ----------

EXTENSION_SUGGESTION_SYSTEM_PROMPT = """\
你是一个简历内容延伸助手。你会收到候选人的多段真实工作/项目经历（每段包含项目
背景描述和已有贡献），以及候选人简历里完全没有出现过的一组目标技能关键词，
还有候选人在其他真实经历里已经写出的结果参考（用于校准量级）。

对每一个关键词，判断它是否和候选人某一段真实经历的技术背景合理相关（例如
"大数据平台"类项目和 Spark 技术栈存在明确的技术关联）。如果相关，从"候选人
经历列表"里选出最合适的一段（用它的序号），构造一条建议：
- action_summary：基于该项目的背景和目的，合理推测候选人使用这项技能可能
  承担的具体行为，要具体、像一条真实简历贡献句里"做了什么"的部分。
- result_summary：给出一个合理的结果/成效表述，量级必须参照"候选人历史结果
  参考"里的真实数据区间，不能给出明显超出候选人历史量级的夸张数字；没有
  合适的数值表达也可以是定性表述。
- rationale：一两句话说明为什么认为这项技能和选中的经历背景相关。

如果对某个关键词找不到任何合理相关的经历，就把 plausible 填 false，不要为
明显无关的技能强行编造关联，也不需要再填其他字段。

严格按下面的 JSON 结构输出，suggestions 数组要覆盖输入的每一个关键词：
{
  "suggestions": [
    {
      "keyword": "...",
      "plausible": true 或 false,
      "position_index": 对应"候选人经历列表"里的序号（从 1 开始，plausible=false 时可省略）,
      "action_summary": "...",
      "result_summary": "...",
      "rationale": "..."
    }
  ]
}
"""


def _historical_results(positions: list[ExperienceEntry]) -> list[str]:
    return [
        b.result_summary
        for position in positions
        for b in position.bullets
        if b.result_summary
    ]


def generate_extension_suggestions(
    positions: list[ExperienceEntry],
    keywords: list[str],
    llm_client: LLMClient,
) -> list[dict]:
    """对 `keywords` 里的每一个关键词，尝试构造一条延伸建议三元组。返回的建议
    还没有经过用户确认，调用方必须把它们作为"待确认"呈现，不能直接写入简历。
    """
    if not keywords:
        return []

    numbered_positions = list(enumerate(positions, start=1))
    positions_text = "\n".join(
        f"{i}. {p.position_title or ''} / {p.project_name or ''}\n"
        f"   背景：{p.background_notes or '（暂无项目背景描述）'}\n"
        f"   已有贡献：{'; '.join(b.content for b in p.bullets) or '（暂无）'}"
        for i, p in numbered_positions
    ) or "（候选人暂无任何工作经历）"
    history_results = _historical_results(positions)
    results_text = "\n".join(f"- {r}" for r in history_results) or "（暂无历史结果数据可参考）"

    user_prompt = (
        f"候选人经历列表：\n{positions_text}\n\n"
        f"候选人历史结果参考：\n{results_text}\n\n"
        f"目标技能关键词：{', '.join(keywords)}"
    )
    result = llm_client.complete_json(EXTENSION_SUGGESTION_SYSTEM_PROMPT, user_prompt)
    raw_suggestions = result.get("suggestions") or []

    position_by_index = {i: p for i, p in numbered_positions}
    suggestions: list[dict] = []
    for item in raw_suggestions:
        if not isinstance(item, dict) or not item.get("plausible"):
            continue
        position = position_by_index.get(item.get("position_index"))
        if position is None:
            continue
        suggestions.append(
            {
                "keyword": item.get("keyword"),
                "experience_entry_id": position.id,
                "position_title": position.position_title,
                "project_name": position.project_name,
                "action_summary": item.get("action_summary") or "",
                "result_summary": item.get("result_summary"),
                "rationale": item.get("rationale") or "",
            }
        )
    return suggestions


# ---------- 草稿生成（编排，不落库） ----------


@dataclass
class ResumeDraft:
    k_value: int
    hit_items: list[dict] = field(default_factory=list)
    suggestions: list[dict] = field(default_factory=list)  # 待确认，未落库


def build_resume_draft(
    db: Session,
    jd_id: int,
    k_value: int,
    light_client: LLMClient,
    heavy_client: LLMClient,
) -> ResumeDraft:
    jd = db.get(JDRecord, jd_id)
    if jd is None:
        raise JDNotFoundError(f"JD id={jd_id} 不存在")

    positions = (
        db.query(ExperienceEntry).filter(ExperienceEntry.level == ExperienceLevel.POSITION).all()
    )
    jd_parsed = jd.parsed_meta if jd.parsed_meta and jd.parsed_meta.get("key_skills") is not None else None
    if jd_parsed is None:
        # 还没跑过 /analyze（或规则解析出的 extra_meta 里没有 key_skills）时,
        # 这里现取现用,并且和 analysis.analyze_jd 一样合并回 parsed_meta,
        # 避免同一份 JD 被重复解析。
        jd_parsed = structure_jd_text(jd, light_client)
        merged_meta = dict(jd.parsed_meta or {})
        merged_meta.update(jd_parsed)
        jd.parsed_meta = merged_meta
        db.commit()

    known_companies = _known_companies(positions)

    jd_keywords = collect_jd_keywords(jd_parsed)
    hits = find_hit_bullets(jd_keywords, positions)
    hit_items = rewrite_hit_bullets(hits, jd, heavy_client) if hits else []
    if hit_items:
        hit_items = _validate_and_repair_hit_items(
            hits, hit_items, jd, known_companies, light_client, heavy_client
        )

    missing = find_missing_keywords(jd_keywords, positions)
    keywords_to_extend = select_keywords_to_extend(missing, k_value)
    suggestions = (
        generate_extension_suggestions(positions, keywords_to_extend, heavy_client)
        if keywords_to_extend
        else []
    )
    if suggestions:
        suggestions = _validate_and_filter_suggestions(
            suggestions, positions, known_companies, light_client, heavy_client
        )

    return ResumeDraft(k_value=k_value, hit_items=hit_items, suggestions=suggestions)


# ---------- 确认后落地：写入 ResumeVersion + 认领技能库 ----------


def _upsert_claimed_skill(db: Session, suggestion: dict, jd_id: int) -> ClaimedSkill:
    existing = (
        db.query(ClaimedSkill)
        .filter(
            ClaimedSkill.experience_entry_id == suggestion["experience_entry_id"],
            ClaimedSkill.skill_name == suggestion["keyword"],
        )
        .one_or_none()
    )
    if existing is None:
        existing = ClaimedSkill(
            skill_name=suggestion["keyword"],
            experience_entry_id=suggestion["experience_entry_id"],
            source_jd_id=jd_id,
        )
        db.add(existing)
    existing.action_summary = suggestion["action_summary"]
    existing.result_summary = suggestion.get("result_summary")
    existing.rationale = suggestion.get("rationale") or existing.rationale or ""
    return existing


def _split_lines(text: str | None) -> list[str]:
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _build_static_sections(db: Session) -> tuple[list[dict], list[dict]]:
    """独立项目 / 教育经历这两块"静态背景信息"——见 profile_service.py 里
    PersonalProject/EducationEntry 的说明，不参与 JD 关键词匹配/K 值裁剪，
    每次生成简历都原样带上全部内容。"""
    projects = [
        {
            "project_name": p.project_name,
            "start_date": p.start_date,
            "end_date": p.end_date,
            "is_current": p.is_current,
            "date_range": format_date_range(p.start_date, p.end_date, p.is_current),
            "bullets": [b.content for b in sorted(p.bullets, key=lambda b: (b.order_index, b.id))],
        }
        for p in get_personal_projects(db)
    ]
    education = [
        {
            "school": e.school,
            "degree": e.degree,
            "location": e.location,
            "start_date": e.start_date,
            "end_date": e.end_date,
            "is_current": e.is_current,
            "date_range": format_date_range(e.start_date, e.end_date, e.is_current),
        }
        for e in _get_education_entries(db)
    ]
    return projects, education


def build_template_context(resume_json: dict) -> dict:
    """把 resume_json（内置 default/compact 两套风格也在用的结构）转换成
    喂给 MD 模板的上下文——两者字段大部分同名，唯一的差异是工作经历下的
    贡献句列表这里改叫 `bullet_items` 而不是 `items`：Jinja2 对 `foo.items`
    做属性访问时会先命中 dict 内置的 `.items()` 方法、而不是取字典里那个
    键（`foo['items']` 才会命中），default.html/compact.html 里全部用的是
    显式的方括号取法所以不受影响，但没法要求用户自定义的 MD 模板也知道
    这个坑，所以这里的上下文干脆换一个不会撞到内置方法名的字段名，从源头
    避免这个问题（resume_template_service.py 模块文档字符串同步记了这一点）。
    """
    experience = [
        {
            "company_name": position.get("company_name"),
            "position_title": position.get("position_title"),
            "project_name": position.get("project_name"),
            "date_range": position.get("date_range"),
            "bullet_items": position.get("items", []),
        }
        for position in resume_json.get("experience", [])
    ]
    projects = [
        {
            "project_name": project.get("project_name"),
            "date_range": project.get("date_range"),
            "bullets": project.get("bullets", []),
        }
        for project in resume_json.get("projects", [])
    ]
    education = [
        {
            "school": edu.get("school"),
            "degree": edu.get("degree"),
            "location": edu.get("location"),
            "date_range": edu.get("date_range"),
        }
        for edu in resume_json.get("education", [])
    ]
    return {
        "basic": resume_json.get("basic") or {},
        "summary": resume_json.get("summary") or [],
        "skills": resume_json.get("skills") or [],
        "experience": experience,
        "projects": projects,
        "education": education,
    }


def _render_markdown(resume_json: dict) -> str:
    """内置 default/compact 两套风格用的固定 Markdown 渲染——纯函数，只依赖
    `resume_json`（不再依赖调用方手上的 profile/items_by_position 这些活的
    ORM 对象），这样 `regenerate_resume_pdf` 换风格时也能拿已经持久化的
    `resume_json` 重新算出完全一样的 markdown_text，不用重新查一遍数据库、
    也不用把 ORM 对象一路传下来。

    和 MD 模板路径（build_template_context + resume_template_service.render_template_markdown）
    是两条独立但读同一份 resume_json 的渲染路径——这里的排版是固定写死的，
    不像 MD 模板那样可以自定义，含义上相当于"内置的默认 MD 模板"。"""
    basic = resume_json.get("basic") or {}
    lines = [f"# {basic.get('full_name') or '（未填写姓名）'}"]
    if basic.get("target_title"):
        lines.append(f"**{basic['target_title']}**")
    contact_bits = [
        b
        for b in [basic.get("current_location"), basic.get("email"), basic.get("phone"), basic.get("github_url"), basic.get("linkedin_url")]
        if b
    ]
    if contact_bits:
        lines.append(" · ".join(contact_bits))
    lines.append("")

    summary = resume_json.get("summary") or []
    if summary:
        lines.append("## 个人总结")
        lines.extend(f"- {line}" for line in summary)
        lines.append("")

    skills = resume_json.get("skills") or []
    if skills:
        lines.append("## 技能")
        lines.extend(f"- {line}" for line in skills)
        lines.append("")

    lines.append("## 工作经历")
    experience = resume_json.get("experience") or []
    if not experience:
        lines.append("（本版本没有命中任何真实经历或已确认的延伸建议）")
    for position in experience:
        header_bits = [b for b in [position.get("company_name"), position.get("position_title") or position.get("project_name")] if b]
        header = " · ".join(header_bits) or "（未命名职位）"
        date_range = position.get("date_range")
        lines.append(f"\n### {header}" + (f"（{date_range}）" if date_range else ""))
        for item in position.get("items", []):
            bullet_line = f"- {item['action_summary']}"
            if item.get("result_summary"):
                bullet_line += f"，{item['result_summary']}"
            lines.append(bullet_line)

    projects = resume_json.get("projects") or []
    if projects:
        lines.append("\n## 独立项目")
        for project in projects:
            date_range = project.get("date_range")
            lines.append(f"\n### {project.get('project_name')}" + (f"（{date_range}）" if date_range else ""))
            for bullet in project.get("bullets", []):
                lines.append(f"- {bullet}")

    education = resume_json.get("education") or []
    if education:
        lines.append("\n## 教育背景")
        for edu in education:
            header_bits = [b for b in [edu.get("school"), edu.get("degree")] if b]
            header = " · ".join(header_bits)
            date_range = edu.get("date_range")
            lines.append(f"- {header}" + (f"（{date_range}）" if date_range else ""))

    return "\n".join(lines)


def confirm_and_finalize(
    db: Session,
    jd_id: int,
    k_value: int,
    hit_items: list[dict],
    accepted_suggestions: list[dict],
    style_id: str = "default",
    resume_template_id: int | None = None,
) -> ResumeVersion:
    """用户对 `build_resume_draft` 产出的建议逐条确认之后调用。`accepted_suggestions`
    只包含用户接受的那些（可能已经被用户编辑过 action_summary/result_summary），
    未接受的建议不会传进来、也就不会以任何形式进入最终简历。

    确认过的每一条会被持久化进 claimed_skill（同一项目下同一个技能名再次确认时
    是更新而不是重复插入），但这个持久化和打分引擎完全无关——compute_score
    不读 claimed_skill 这张表。

    `resume_template_id` 非空时代表用户这次选的是 MD 模板库里的某个模板，
    而不是内置的 default/compact 风格——这时 `style_id` 参数被忽略，
    最终存进去的 `style_id` 恒为 `resume_pdf.MD_TEMPLATE_STYLE_SENTINEL`（见
    该模块文档字符串）。两条路径共用同一份 `resume_json` 构建逻辑,只是
    markdown_text/PDF 的渲染方式不同——模板不存在会在这里提前抛
    `resume_template_service.ResumeTemplateNotFoundError`,不会插入一半
    的 `resume_version`。
    """
    jd = db.get(JDRecord, jd_id)
    if jd is None:
        raise JDNotFoundError(f"JD id={jd_id} 不存在")
    profile = get_or_create_profile_basic(db)

    template = get_resume_template(db, resume_template_id) if resume_template_id is not None else None

    position_ids = {item["experience_entry_id"] for item in [*hit_items, *accepted_suggestions]}
    positions = {
        p.id: p
        for p in db.query(ExperienceEntry).filter(ExperienceEntry.id.in_(position_ids)).all()
    }

    items_by_position: dict[ExperienceEntry, list[dict]] = {}
    for item in hit_items:
        position = positions.get(item["experience_entry_id"])
        if position is None:
            continue
        items_by_position.setdefault(position, []).append(
            {"action_summary": item["action_summary"], "result_summary": item.get("result_summary")}
        )

    for suggestion in accepted_suggestions:
        position = positions.get(suggestion["experience_entry_id"])
        if position is None:
            continue
        _upsert_claimed_skill(db, suggestion, jd_id)
        items_by_position.setdefault(position, []).append(
            {
                "action_summary": suggestion["action_summary"],
                "result_summary": suggestion.get("result_summary"),
            }
        )

    projects, education = _build_static_sections(db)
    resume_json = {
        "basic": {
            "full_name": profile.full_name,
            "target_title": profile.target_title,
            "email": profile.email,
            "phone": profile.phone,
            "current_location": profile.current_location,
            "github_url": profile.github_url,
            "linkedin_url": profile.linkedin_url,
        },
        "summary": _split_lines(profile.resume_summary),
        "skills": _split_lines(profile.skills_text),
        "experience": [
            {
                "company_name": position.parent.company_name if position.parent else None,
                "position_title": position.position_title,
                "project_name": position.project_name,
                "start_date": position.start_date,
                "end_date": position.end_date,
                "is_current": position.is_current,
                "date_range": format_date_range(position.start_date, position.end_date, position.is_current),
                "items": items,
            }
            for position, items in items_by_position.items()
        ],
        "projects": projects,
        "education": education,
    }

    if template is not None:
        markdown_text = render_template_markdown(template.content, build_template_context(resume_json))
        stored_style_id = MD_TEMPLATE_STYLE_SENTINEL
    else:
        markdown_text = _render_markdown(resume_json)
        stored_style_id = style_id

    resume_version = ResumeVersion(
        jd_id=jd_id,
        k_value=k_value,
        style_id=stored_style_id,
        resume_template_id=template.id if template is not None else None,
        resume_json=resume_json,
        markdown_text=markdown_text,
        pdf_path=None,
    )
    db.add(resume_version)
    db.commit()
    db.refresh(resume_version)

    # PDF 渲染需要先拿到 resume_version.id 才能确定文件名，所以放在第一次
    # commit 之后单独做一次；渲染失败不应该让整个"确认简历"操作报错回滚——
    # Markdown/JSON 才是事实来源，PDF 只是它的一种展示形式，缺了它用户仍然
    # 能拿到完整可用的简历内容,只是没有 PDF 下载链接。
    try:
        if template is not None:
            pdf_path = save_markdown_resume_pdf(resume_version.id, markdown_text)
        else:
            pdf_path = save_resume_pdf(resume_version.id, resume_json, style_id)
        resume_version.pdf_path = str(pdf_path)
        db.commit()
        db.refresh(resume_version)
    except Exception:  # noqa: BLE001 - PDF 是附加产物，失败不应该拖垮整个确认流程
        db.rollback()
        logger.exception("简历 PDF 渲染失败（resume_version_id=%s），已跳过，Markdown/JSON 不受影响", resume_version.id)

    return resume_version


# ---------- Phase 5：简历风格自定义——换个风格重新渲染 PDF，不重新走 LLM ----------


def regenerate_resume_pdf(
    db: Session, resume_version_id: int, style_id: str | None = None, resume_template_id: int | None = None
) -> ResumeVersion:
    """已经生成过的简历版本，用户想换一套风格/模板看看效果，不需要重新走
    一遍 K 值/延伸建议确认这套完整流程——`resume_json` 已经是持久化好的
    事实来源，换风格只是把同一份内容重新渲染一次，属于纯本地渲染，不消耗
    任何 LLM 调用，所以允许用户随便换着试。`style_id`/`resume_template_id`
    互斥，传哪个就走哪条路径（都不传按 style_id="default" 处理）。

    `markdown_text` 每次都会跟着重新算一遍（`_render_markdown`/
    `render_template_markdown` 都是只读 `resume_json` 的纯函数），不是
    "只有换模板才更新"——这不代表在 default/compact 两个内置风格之间切换
    会改变 markdown_text 的内容：`resume_json` 没变,`_render_markdown` 对
    同样的输入必然算出同样的输出,纯函数意义上和"完全不碰 markdown_text"
    是等价的,但换成 MD 模板（或者换一个不同的 MD 模板）确实会让
    markdown_text 变成模板渲染出来的新内容,这是有意的——模板本身就是在
    决定"这份简历的 Markdown 应该长什么样"。

    这里故意不像 `confirm_and_finalize` 那样自己吞掉 PDF 渲染失败的异常：
    那边"生成简历"是一个更大的操作,PDF 只是附带产物,失败了不该拖累整个
    确认流程;这里"换个风格重新生成 PDF"本身就是用户点的这一个动作、没有
    更大的操作需要保护,失败了应该让调用方（Dashboard 路由）感知到并提示
    用户,而不是静默地什么都没发生。异常抛出时下面的赋值根本不会执行,
    所以旧的 `style_id`/`pdf_path`/`markdown_text` 会保持原样——不会因为
    一次失败的"换风格"尝试,把用户已经拥有的、能正常下载的旧 PDF 意外弄丢。
    """
    resume_version = db.get(ResumeVersion, resume_version_id)
    if resume_version is None:
        raise ResumeVersionNotFoundError(f"resume_version id={resume_version_id} 不存在")
    resume_json = resume_version.resume_json or {}

    if resume_template_id is not None:
        template = get_resume_template(db, resume_template_id)  # 不存在会抛 ResumeTemplateNotFoundError
        markdown_text = render_template_markdown(template.content, build_template_context(resume_json))
        pdf_path = save_markdown_resume_pdf(resume_version.id, markdown_text)
        resume_version.style_id = MD_TEMPLATE_STYLE_SENTINEL
        resume_version.resume_template_id = template.id
    else:
        style_id = style_id or "default"
        if style_id not in AVAILABLE_RESUME_STYLES:
            raise UnknownResumeStyleError(f"未知的简历风格：{style_id}")
        markdown_text = _render_markdown(resume_json)
        pdf_path = save_resume_pdf(resume_version.id, resume_json, style_id)
        resume_version.style_id = style_id
        resume_version.resume_template_id = None

    resume_version.markdown_text = markdown_text
    resume_version.pdf_path = str(pdf_path)
    db.commit()
    db.refresh(resume_version)
    return resume_version
