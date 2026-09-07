"""Phase 1：打分引擎核心——compute_score 是纯函数，必须对同样的输入
无论调用多少次都产出完全一致的结果（对应用户的硬性要求："打分必须整体
稳定一致且客观，不能出现前后标准出现偏差"）。

这里既锁定"确定性"这条根本性质，也覆盖规则本身的边界情况：
年限差刚好到年、超过封顶、加分技能数量封顶、学历+清关同时不满足时
不重复扣分、总分被 clamp 到 [0, 100]。
"""

from __future__ import annotations

from app.models.tables import ProfileBasic
from app.services.scoring import (
    HARD_REQUIREMENT_PENALTY,
    PLUS_SKILL_BONUS,
    PLUS_SKILL_BONUS_CAP,
    YEARS_GAP_PENALTY_CAP,
    YEARS_GAP_PENALTY_PER_YEAR,
    compute_score,
)


def _profile(years_experience=3.0, education="Bachelor's degree"):
    return ProfileBasic(id=1, years_experience=years_experience, education=education)


def test_compute_score_is_fully_deterministic_across_repeated_calls():
    profile = _profile(years_experience=3.0)
    jd_parsed = {
        "required_years": 5,
        "required_education": "Bachelor's degree",
        "required_clearance": False,
        "plus_skills": ["Kubernetes", "Rust"],
    }
    llm_judgement = {
        "skill_fit_score": 72,
        "meets_education_requirement": True,
        "meets_clearance_requirement": True,
        "plus_skills_matched": ["Kubernetes"],
        "strengths": ["strong backend background"],
        "weaknesses": ["no Rust experience"],
    }

    results = [compute_score(profile, jd_parsed, llm_judgement) for _ in range(5)]
    first = results[0]
    for r in results[1:]:
        assert r.skill_fit_score == first.skill_fit_score
        assert r.hard_requirement_penalty == first.hard_requirement_penalty
        assert r.flexible_adjustment == first.flexible_adjustment
        assert r.total_score == first.total_score
        assert r.breakdown == first.breakdown
        assert r.strengths == first.strengths
        assert r.weaknesses == first.weaknesses


def test_skill_fit_score_is_clamped_to_0_100():
    profile = _profile()
    result_high = compute_score(profile, {}, {"skill_fit_score": 150})
    result_low = compute_score(profile, {}, {"skill_fit_score": -20})
    assert result_high.skill_fit_score == 100
    assert result_low.skill_fit_score == 0


def test_hard_requirement_penalty_applied_when_education_not_met():
    profile = _profile()
    jd_parsed = {"required_education": "Master's degree"}
    llm_judgement = {"skill_fit_score": 80, "meets_education_requirement": False}

    result = compute_score(profile, jd_parsed, llm_judgement)

    assert result.hard_requirement_penalty == -HARD_REQUIREMENT_PENALTY
    assert result.total_score == 80 - HARD_REQUIREMENT_PENALTY


def test_no_hard_penalty_when_jd_has_no_education_requirement():
    profile = _profile()
    jd_parsed = {"required_education": None}
    llm_judgement = {"skill_fit_score": 80, "meets_education_requirement": False}

    result = compute_score(profile, jd_parsed, llm_judgement)
    assert result.hard_requirement_penalty == 0


def test_education_and_clearance_penalties_do_not_stack():
    """学历和清关都不满足时，硬性扣分只扣一次 -20，不叠加成 -40。"""
    profile = _profile()
    jd_parsed = {"required_education": "Master's degree", "required_clearance": True}
    llm_judgement = {
        "skill_fit_score": 80,
        "meets_education_requirement": False,
        "meets_clearance_requirement": False,
    }

    result = compute_score(profile, jd_parsed, llm_judgement)
    assert result.hard_requirement_penalty == -HARD_REQUIREMENT_PENALTY
    assert result.total_score == 80 - HARD_REQUIREMENT_PENALTY


def test_years_gap_penalty_rounds_up_to_whole_years():
    profile = _profile(years_experience=3.0)
    jd_parsed = {"required_years": 5}  # gap = 2.0，正好整数年
    llm_judgement = {"skill_fit_score": 90}

    result = compute_score(profile, jd_parsed, llm_judgement)
    expected_penalty = YEARS_GAP_PENALTY_PER_YEAR * 2
    assert result.flexible_adjustment == -expected_penalty
    assert result.total_score == 90 - expected_penalty


def test_years_gap_penalty_rounds_up_partial_year():
    profile = _profile(years_experience=3.5)
    jd_parsed = {"required_years": 5}  # gap = 1.5 -> ceil 到 2 年
    llm_judgement = {"skill_fit_score": 90}

    result = compute_score(profile, jd_parsed, llm_judgement)
    assert result.flexible_adjustment == -(YEARS_GAP_PENALTY_PER_YEAR * 2)


def test_years_gap_penalty_is_capped():
    profile = _profile(years_experience=0.0)
    jd_parsed = {"required_years": 20}  # gap = 20 年，远超封顶
    llm_judgement = {"skill_fit_score": 90}

    result = compute_score(profile, jd_parsed, llm_judgement)
    assert result.flexible_adjustment == -YEARS_GAP_PENALTY_CAP


def test_no_years_penalty_when_candidate_meets_or_exceeds_requirement():
    profile = _profile(years_experience=6.0)
    jd_parsed = {"required_years": 5}
    llm_judgement = {"skill_fit_score": 90}

    result = compute_score(profile, jd_parsed, llm_judgement)
    assert result.flexible_adjustment == 0


def test_no_years_penalty_when_required_years_or_candidate_years_missing():
    profile = _profile(years_experience=None)
    jd_parsed = {"required_years": 5}
    llm_judgement = {"skill_fit_score": 90}
    result = compute_score(profile, jd_parsed, llm_judgement)
    assert result.flexible_adjustment == 0

    profile2 = _profile(years_experience=1.0)
    jd_parsed2 = {"required_years": None}
    result2 = compute_score(profile2, jd_parsed2, llm_judgement)
    assert result2.flexible_adjustment == 0


def test_plus_skill_bonus_scales_with_matched_count_and_is_capped():
    profile = _profile()
    llm_judgement_two = {"skill_fit_score": 50, "plus_skills_matched": ["A", "B"]}
    result_two = compute_score(profile, {}, llm_judgement_two)
    assert result_two.flexible_adjustment == PLUS_SKILL_BONUS * 2

    # 匹配数量很多时，加分被封顶
    many_skills = [f"skill_{i}" for i in range(20)]
    llm_judgement_many = {"skill_fit_score": 50, "plus_skills_matched": many_skills}
    result_many = compute_score(profile, {}, llm_judgement_many)
    assert result_many.flexible_adjustment == PLUS_SKILL_BONUS_CAP


def test_total_score_clamped_to_100_even_with_bonuses():
    profile = _profile(years_experience=10.0)
    jd_parsed = {"required_years": 1}
    llm_judgement = {"skill_fit_score": 100, "plus_skills_matched": ["A", "B", "C", "D", "E", "F"]}

    result = compute_score(profile, jd_parsed, llm_judgement)
    assert result.total_score == 100


def test_total_score_clamped_to_0_even_with_heavy_penalties():
    profile = _profile(years_experience=0.0)
    jd_parsed = {"required_years": 20, "required_education": "PhD"}
    llm_judgement = {"skill_fit_score": 5, "meets_education_requirement": False}

    result = compute_score(profile, jd_parsed, llm_judgement)
    assert result.total_score == 0
