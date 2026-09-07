"""把 JD 解析 + 打分串起来的编排逻辑，同时维护 JDRecord 的状态机。

light_client / heavy_client 由调用方传入（路由层通过 FastAPI 依赖注入拿到，
测试里直接传 FakeLLMClient），这个函数本身不关心具体是哪个 LLM 实现，也方便
脱离 FastAPI、脱离真实网络单独做单元测试。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.llm_client import LLMClient
from app.models.tables import ExperienceEntry, ExperienceLevel, JDRecord, JDStatus, MatchScore
from app.services.jd_ingest import structure_jd_text
from app.services.profile_service import get_or_create_profile_basic
from app.services.scoring import run_scoring_with_llm


class JDNotFoundError(RuntimeError):
    pass


def analyze_jd(db: Session, jd_id: int, light_client: LLMClient, heavy_client: LLMClient) -> MatchScore:
    jd = db.get(JDRecord, jd_id)
    if jd is None:
        raise JDNotFoundError(f"JD id={jd_id} 不存在")

    jd.status = JDStatus.ANALYZING
    db.commit()

    try:
        jd_parsed = structure_jd_text(jd, light_client)
        # 规则解析出的 extra_meta（申请人数、发布时间等）保留，和 LLM 解析结果合并
        merged_meta = dict(jd.parsed_meta or {})
        merged_meta.update(jd_parsed)
        jd.parsed_meta = merged_meta

        profile = get_or_create_profile_basic(db)
        experience_entries = (
            db.query(ExperienceEntry).filter(ExperienceEntry.level == ExperienceLevel.COMPANY).all()
        )

        score_result = run_scoring_with_llm(profile, experience_entries, jd, jd_parsed, heavy_client)

        match_score = MatchScore(
            jd_id=jd.id,
            skill_fit_score=score_result.skill_fit_score,
            hard_requirement_penalty=score_result.hard_requirement_penalty,
            flexible_adjustment=score_result.flexible_adjustment,
            total_score=score_result.total_score,
            breakdown=score_result.breakdown,
            strengths=score_result.strengths,
            weaknesses=score_result.weaknesses,
            model_used=getattr(heavy_client, "model", None),
            prompt_version="v1",
        )
        db.add(match_score)
        jd.status = JDStatus.ANALYZED
        db.commit()
        db.refresh(match_score)
        return match_score
    except Exception:
        db.rollback()
        jd = db.get(JDRecord, jd_id)
        jd.status = JDStatus.PENDING
        db.commit()
        raise
