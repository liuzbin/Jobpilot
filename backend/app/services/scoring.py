"""
JD 匹配打分引擎。

核心设计原则（对应实施方案"四、简历匹配部分"里"打分必须整体稳定一致"的要求）：
LLM 只负责"从画像和 JD 里抽取/判断事实"这一步（技能语义契合度打分、是否满足
学历/清关这类需要语义判断的硬性要求），所有的加减分数值计算都由这里的纯 Python
代码完成，不假手于 LLM。这样只要 LLM 抽取出的事实稳定，最终分数就是完全确定性的、
可复现的——不会出现"同一份 JD 和画像，前后两次打分分数不一样"的问题。

年限差、加分项数量这些"数值题"完全交给代码算，规避了 LLM 心算数字容易出错、
且同一道题目每次心算结果可能不一致的问题。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.llm_client import LLMClient
from app.models.tables import ExperienceBullet, ExperienceEntry, ExperienceLevel, JDRecord, ProfileBasic

SCORING_SYSTEM_PROMPT = """\
你的任务是严格按照给定的评分维度，评估候选人画像与目标JD的匹配度。你只负责给出
语义判断和证据，不需要自己计算最终分数（后续有专门的程序按规则计算）。

候选人画像里的 `basic.additional_notes` 字段（如果非空）是候选人自己填写的
附加信息，比如自我评价的优势/劣势、求职偏好等，不是某段具体经历的事实描述。
请把它当成候选人本人提供的补充背景来理解，在判断 skill_fit_score、
matched_key_skills、strengths、weaknesses 时加以考虑——但不要凭空把候选人
自己说的话直接当成"命中的关键技能"或者不加验证地照抄进 strengths，仍然要
结合画像里的真实经历判断是否站得住脚。

请基于候选人画像（JSON）和目标 JD 的结构化字段与原文，严格按下面的 JSON 格式输出：
{
  "skill_fit_score": 0到100之间的整数，反映候选人的关键字和经验描述与JD核心职责的重合度,
  "matched_key_skills": ["候选人画像里确实体现出的、且JD强调的关键技能"],
  "meets_education_requirement": true或false（如果JD没有明确学历要求，填true）,
  "meets_clearance_requirement": true或false（如果JD没有明确清关/工签要求，填true）,
  "plus_skills_matched": ["候选人画像里能体现出的、属于JD中Plus/Preferred技能列表里的技能"],
  "strengths": ["候选人相对这份JD的优势点，2到4条"],
  "weaknesses": ["候选人相对这份JD的劣势点，2到4条"]
}
"""

HARD_REQUIREMENT_PENALTY = 20
YEARS_GAP_PENALTY_PER_YEAR = 5
YEARS_GAP_PENALTY_CAP = 15
PLUS_SKILL_BONUS = 2
PLUS_SKILL_BONUS_CAP = 10


def build_profile_context(profile: ProfileBasic, experience_entries: list[ExperienceEntry]) -> dict:
    """把画像整理成喂给 LLM 的 JSON 结构。"""
    companies = []
    for company in experience_entries:
        if company.level != ExperienceLevel.COMPANY:
            continue
        positions = []
        for pos in company.children:
            bullets: list[ExperienceBullet] = pos.bullets
            positions.append(
                {
                    "position_title": pos.position_title,
                    "project_name": pos.project_name,
                    "start_date": pos.start_date,
                    "end_date": pos.end_date,
                    "is_current": pos.is_current,
                    "bullets": [b.content for b in bullets],
                }
            )
        companies.append({"company_name": company.company_name, "positions": positions})

    return {
        "basic": {
            "target_title": profile.target_title,
            "education": profile.education,
            "years_experience": profile.years_experience,
            "current_location": profile.current_location,
            "target_location": profile.target_location,
            "work_authorization": profile.work_authorization,
            # 打磨阶段后新增：候选人自己填写的附加信息（自我评价的优势/
            # 劣势、求职偏好等），见 ProfileBasic.additional_notes 的表
            # 注释和上面 SCORING_SYSTEM_PROMPT 里对应的说明——这是唯一一
            # 处会实际进入 LLM 语义判断的"静态背景信息"，compute_score
            # 的确定性数值计算完全不读这个字段，只通过 llm_judgement 的
            # strengths/weaknesses 间接体现。
            "additional_notes": profile.additional_notes,
        },
        "experience": companies,
    }


@dataclass
class ScoreBreakdownItem:
    type: str
    detail: str
    delta: float


@dataclass
class ScoreResult:
    skill_fit_score: float
    hard_requirement_penalty: float
    flexible_adjustment: float
    total_score: float
    breakdown: list[dict] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    raw_llm_result: dict = field(default_factory=dict)


def compute_score(profile: ProfileBasic, jd_parsed: dict, llm_judgement: dict) -> ScoreResult:
    """纯函数：给定画像、JD 解析结果、LLM 的语义判断结果，确定性地算出最终分数。
    同样的三个输入，无论调用多少次，输出必须完全一致——这条是自动化测试会锁住的核心断言。
    """
    breakdown: list[dict] = []

    skill_fit_score = max(0, min(100, float(llm_judgement.get("skill_fit_score", 0))))

    hard_penalty = 0.0
    required_education = jd_parsed.get("required_education")
    if required_education and not llm_judgement.get("meets_education_requirement", True):
        hard_penalty -= HARD_REQUIREMENT_PENALTY
        breakdown.append(
            {
                "type": "hard_requirement",
                "detail": f"JD 要求学历「{required_education}」，候选人不满足",
                "delta": -HARD_REQUIREMENT_PENALTY,
            }
        )
    if jd_parsed.get("required_clearance") and not llm_judgement.get("meets_clearance_requirement", True):
        # 学历和清关都不满足时，硬性扣分不重复叠加，保持在 -20（避免对候选人过度惩罚）
        if hard_penalty == 0:
            hard_penalty -= HARD_REQUIREMENT_PENALTY
            breakdown.append(
                {
                    "type": "hard_requirement",
                    "detail": f"JD 要求清关/工签「{jd_parsed.get('clearance_description') or ''}」，候选人不满足",
                    "delta": -HARD_REQUIREMENT_PENALTY,
                }
            )
        else:
            breakdown.append(
                {
                    "type": "hard_requirement",
                    "detail": "同时不满足清关/工签要求（已与学历扣分合并，不重复扣分）",
                    "delta": 0,
                }
            )

    flexible_adjustment = 0.0
    required_years = jd_parsed.get("required_years")
    candidate_years = profile.years_experience
    if required_years is not None and candidate_years is not None:
        gap = float(required_years) - float(candidate_years)
        if gap > 0:
            import math

            years_penalty = min(YEARS_GAP_PENALTY_CAP, YEARS_GAP_PENALTY_PER_YEAR * math.ceil(gap))
            flexible_adjustment -= years_penalty
            breakdown.append(
                {
                    "type": "years_gap",
                    "detail": f"JD 要求 {required_years} 年，候选人 {candidate_years} 年，差 {gap:.1f} 年",
                    "delta": -years_penalty,
                }
            )

    plus_matched = llm_judgement.get("plus_skills_matched") or []
    if plus_matched:
        bonus = min(PLUS_SKILL_BONUS_CAP, PLUS_SKILL_BONUS * len(plus_matched))
        flexible_adjustment += bonus
        breakdown.append(
            {
                "type": "plus_skills",
                "detail": f"命中加分技能: {', '.join(plus_matched)}",
                "delta": bonus,
            }
        )

    total = max(0.0, min(100.0, skill_fit_score + hard_penalty + flexible_adjustment))

    return ScoreResult(
        skill_fit_score=skill_fit_score,
        hard_requirement_penalty=hard_penalty,
        flexible_adjustment=flexible_adjustment,
        total_score=total,
        breakdown=breakdown,
        strengths=llm_judgement.get("strengths") or [],
        weaknesses=llm_judgement.get("weaknesses") or [],
        raw_llm_result=llm_judgement,
    )


def run_scoring_with_llm(
    profile: ProfileBasic,
    experience_entries: list[ExperienceEntry],
    jd: JDRecord,
    jd_parsed: dict,
    llm_client: LLMClient,
) -> ScoreResult:
    profile_context = build_profile_context(profile, experience_entries)
    user_prompt = (
        f"候选人画像:\n{profile_context}\n\n目标岗位JD结构化字段:\n{jd_parsed}\n\n目标岗位JD原文:\n{jd.description_raw}"
    )
    llm_judgement = llm_client.complete_json(SCORING_SYSTEM_PROMPT, user_prompt)
    return compute_score(profile, jd_parsed, llm_judgement)
