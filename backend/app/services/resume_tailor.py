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

本模块目前只产出结构化 JSON + 纯文本 Markdown 预览，PDF 渲染
（Jinja2 + WeasyPrint、简历风格模板）是这个阶段的后续增量，暂未实现。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.llm_client import LLMClient
from app.models.tables import (
    ClaimedSkill,
    ExperienceEntry,
    ExperienceLevel,
    JDRecord,
    ProfileBasic,
    ResumeVersion,
)
from app.services.jd_ingest import structure_jd_text
from app.services.profile_service import get_or_create_profile_basic


class JDNotFoundError(RuntimeError):
    pass


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

    jd_keywords = collect_jd_keywords(jd_parsed)
    hits = find_hit_bullets(jd_keywords, positions)
    hit_items = rewrite_hit_bullets(hits, jd, heavy_client) if hits else []

    missing = find_missing_keywords(jd_keywords, positions)
    keywords_to_extend = select_keywords_to_extend(missing, k_value)
    suggestions = (
        generate_extension_suggestions(positions, keywords_to_extend, heavy_client)
        if keywords_to_extend
        else []
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


def _render_markdown(profile: ProfileBasic, items_by_position: dict) -> str:
    lines = [f"# {profile.full_name or '（未填写姓名）'}"]
    if profile.target_title:
        lines.append(f"**{profile.target_title}**")
    lines.append("")
    lines.append("## 工作经历")
    for position, items in items_by_position.items():
        header = " / ".join(filter(None, [position.position_title, position.project_name]))
        lines.append(f"\n### {header}")
        for item in items:
            bullet_line = f"- {item['action_summary']}"
            if item.get("result_summary"):
                bullet_line += f"，{item['result_summary']}"
            lines.append(bullet_line)
    return "\n".join(lines)


def confirm_and_finalize(
    db: Session,
    jd_id: int,
    k_value: int,
    hit_items: list[dict],
    accepted_suggestions: list[dict],
    style_id: str = "default",
) -> ResumeVersion:
    """用户对 `build_resume_draft` 产出的建议逐条确认之后调用。`accepted_suggestions`
    只包含用户接受的那些（可能已经被用户编辑过 action_summary/result_summary），
    未接受的建议不会传进来、也就不会以任何形式进入最终简历。

    确认过的每一条会被持久化进 claimed_skill（同一项目下同一个技能名再次确认时
    是更新而不是重复插入），但这个持久化和打分引擎完全无关——compute_score
    不读 claimed_skill 这张表。
    """
    jd = db.get(JDRecord, jd_id)
    if jd is None:
        raise JDNotFoundError(f"JD id={jd_id} 不存在")
    profile = get_or_create_profile_basic(db)

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

    resume_json = {
        "basic": {
            "full_name": profile.full_name,
            "target_title": profile.target_title,
            "email": profile.email,
            "phone": profile.phone,
        },
        "experience": [
            {
                "position_title": position.position_title,
                "project_name": position.project_name,
                "items": items,
            }
            for position, items in items_by_position.items()
        ],
    }
    markdown_text = _render_markdown(profile, items_by_position)

    resume_version = ResumeVersion(
        jd_id=jd_id,
        k_value=k_value,
        style_id=style_id,
        resume_json=resume_json,
        markdown_text=markdown_text,
        pdf_path=None,  # PDF 渲染（Jinja2 + WeasyPrint 风格模板）留待后续增量实现
    )
    db.add(resume_version)
    db.commit()
    db.refresh(resume_version)
    return resume_version
