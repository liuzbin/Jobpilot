"""插件（Chrome Extension）专用的 REST 接口。

和 `routes_system.py` 里的 `/api/health` `/api/status` 一样走配对鉴权
（`require_paired_request`：校验 `X-JobPilot-Token` + Origin 必须是
`chrome-extension://` 开头），区别是这里放的是插件真正要用来"做事"的业务接口，
而不是连接状态探测。Phase 3：插件在职位详情页（当前只做了 LinkedIn）解析出
JD 信息后，一键发过来入库，直接复用 Phase 1 已经跑通的 `jd_ingest.create_jd`，
和 Dashboard 手动粘贴 JD 走的是完全同一条入库逻辑，入库之后仍然停在
`pending` 状态，用户在 Dashboard 里点"分析"才会真正调用 LLM 打分——插件这
一步只负责"送进来"，不越权替用户触发消耗 API 配额的分析动作。

Phase 4：自动化填表相关的接口。同样遵循"插件只负责执行，决策逻辑在本地
App 里"的分工——插件侧的 content script 只做"扫描页面上有哪些表单字段"和
"把某个字段的值真正写进 DOM"这两件机械的事，具体"每个字段该填什么、
要不要填"完全由 `POST /api/autofill/plan` 这个接口调用
`app.services.autofill.build_autofill_plan` 算出来,包括本文件顶部反复
强调过的合规安全限制,也全部在这一层的服务函数里统一收口,插件侧不需要
（也不应该）自己重新实现一遍这些判断逻辑。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_paired_request
from app.api.deps_llm import get_light_client
from app.core.config import get_settings
from app.core.db import get_db
from app.core.llm_client import LLMClient
from app.models.tables import JDRecord, JDStatus, QASource, ResumeVersion
from app.services.autofill import build_autofill_plan
from app.services.jd_ingest import create_jd
from app.services.profile_service import merge_parsed_experience
from app.services.qa_bank_service import add_qa_entry

router = APIRouter(prefix="/api", dependencies=[Depends(require_paired_request)])


class ExtensionJobSubmission(BaseModel):
    company: str | None = None
    title: str | None = None
    description_raw: str
    location: str | None = None
    source_url: str | None = None
    extra_meta_raw: str | None = None


class ExtensionJobCreatedResponse(BaseModel):
    jd_id: int
    dashboard_url: str


@router.post("/jobs", response_model=ExtensionJobCreatedResponse)
def submit_job(payload: ExtensionJobSubmission, db: Session = Depends(get_db)) -> ExtensionJobCreatedResponse:
    settings = get_settings()
    description_raw = payload.description_raw.strip()
    if not description_raw:
        # 页面结构识别失败、抓到空描述的兜底：不要往库里塞一条没有任何内容的 JD 记录，
        # 让插件侧能明确提示用户"这个页面没抓到正文，试试手动复制粘贴"。
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="职位描述为空，抓取失败")

    jd = create_jd(
        db,
        company=(payload.company or "").strip() or None,
        title=(payload.title or "").strip() or None,
        description_raw=description_raw,
        location=(payload.location or "").strip() or None,
        source_url=(payload.source_url or "").strip() or None,
        extra_meta_raw=(payload.extra_meta_raw or "").strip() or None,
    )
    dashboard_url = f"http://{settings.host}:{settings.port}/dashboard/jobs/{jd.id}"
    return ExtensionJobCreatedResponse(jd_id=jd.id, dashboard_url=dashboard_url)


# ---------- LinkedIn 画像导入（打磨阶段后新增） ----------
#
# 插件侧在侧边栏输入 LinkedIn 个人主页地址后，会把该 tab 导航过去，用内容
# 脚本（content_scripts/linkedin_profile_parser.js，纯 DOM 抓取，不需要
# LLM）解析出下面这套结构，再经由 background service worker 发到这个
# 接口——和插件抓 JD 送 /api/jobs 是同一套"内容脚本只管抓取，真正的网络
# 请求只在 background 里发生"的分工。
#
# 数据结构故意和 profile_service.merge_parsed_experience 已经在用的
# `parsed` 字典完全同构（companies/positions/bullets、education_entries、
# projects 的字段名和上传简历解析出来的结构一模一样）——这样可以直接复用
# 同一套合并逻辑（公司/职位的精确+模糊匹配、贡献句去重、教育经历/独立
# 项目精确去重），不需要为 LinkedIn 这个数据源单独再写一遍合并规则。
# 唯一的新增字段是 `skills`（结构化技能标签列表,见 ProfileSkill 表注释）
# 和 `projects[].company_tag`（独立项目关联公司的自由文本标签,见
# PersonalProject 表注释）。


class ExtensionLinkedInBasicInfo(BaseModel):
    full_name: str | None = None
    target_title: str | None = None
    current_location: str | None = None
    linkedin_url: str | None = None
    resume_summary: str | None = None  # LinkedIn "About" 板块 -> 个人简介


class ExtensionLinkedInEducationEntry(BaseModel):
    school: str
    degree: str | None = None
    location: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False


class ExtensionLinkedInProject(BaseModel):
    project_name: str
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False
    company_tag: str | None = None
    bullets: list[str] = []


class ExtensionLinkedInPosition(BaseModel):
    position_title: str | None = None
    project_name: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False
    bullets: list[str] = []


class ExtensionLinkedInCompany(BaseModel):
    company_name: str
    positions: list[ExtensionLinkedInPosition] = []


class ExtensionLinkedInProfileSubmission(BaseModel):
    basic: ExtensionLinkedInBasicInfo | None = None
    skills: list[str] = []
    education_entries: list[ExtensionLinkedInEducationEntry] = []
    projects: list[ExtensionLinkedInProject] = []
    companies: list[ExtensionLinkedInCompany] = []
    source_url: str | None = None


class ExtensionLinkedInProfileResponse(BaseModel):
    dashboard_url: str
    companies_added: int
    positions_added: int
    bullets_added: int
    education_added: int
    projects_added: int
    project_bullets_added: int
    skills_added: int
    basic_fields_filled: list[str]
    has_conflicts: bool


@router.post("/linkedin-profile", response_model=ExtensionLinkedInProfileResponse)
def submit_linkedin_profile(
    payload: ExtensionLinkedInProfileSubmission, db: Session = Depends(get_db)
) -> ExtensionLinkedInProfileResponse:
    settings = get_settings()
    has_any_content = bool(
        (payload.basic and payload.basic.model_dump(exclude_none=True))
        or payload.skills
        or payload.education_entries
        or payload.projects
        or payload.companies
    )
    if not has_any_content:
        # 和 submit_job 的空描述兜底是同一个道理：页面结构识别失败、什么都
        # 没抓到时，不要往库里塞一次空的合并操作，让插件侧能明确提示用户
        # "这个页面没抓到内容，确认一下是不是本人的 LinkedIn 主页"。
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="LinkedIn 主页解析结果为空，抓取失败")

    parsed = {
        "basic": payload.basic.model_dump(exclude_none=True) if payload.basic else {},
        "skills": payload.skills,
        "education_entries": [e.model_dump() for e in payload.education_entries],
        "projects": [p.model_dump() for p in payload.projects],
        "companies": [c.model_dump() for c in payload.companies],
    }
    result = merge_parsed_experience(db, parsed)
    dashboard_url = f"http://{settings.host}:{settings.port}/dashboard/profile"
    return ExtensionLinkedInProfileResponse(
        dashboard_url=dashboard_url,
        companies_added=result.companies_added,
        positions_added=result.positions_added,
        bullets_added=result.bullets_added,
        education_added=result.education_added,
        projects_added=result.projects_added,
        project_bullets_added=result.project_bullets_added,
        skills_added=result.skills_added,
        basic_fields_filled=result.basic_fields_filled,
        has_conflicts=bool(result.bullet_conflicts or result.position_field_conflicts),
    )


# ---------- Phase 4：自动化填表 ----------


class ExtensionFormFieldOption(BaseModel):
    value: str | None = None
    label: str | None = None


class ExtensionFormField(BaseModel):
    field_id: str
    label: str | None = None
    input_type: str = "text"
    options: list[ExtensionFormFieldOption] = []


class ExtensionAutofillPlanRequest(BaseModel):
    jd_id: int | None = None
    fields: list[ExtensionFormField]


class ExtensionAutofillAction(BaseModel):
    field_id: str
    action: str
    value: str | None = None
    source: str | None = None
    reason: str | None = None
    similarity: float | None = None


class ExtensionAutofillPlanResponse(BaseModel):
    plan: list[ExtensionAutofillAction]


@router.post("/autofill/plan", response_model=ExtensionAutofillPlanResponse)
def autofill_plan(
    payload: ExtensionAutofillPlanRequest,
    db: Session = Depends(get_db),
    light_client: LLMClient | None = Depends(get_light_client),
) -> ExtensionAutofillPlanResponse:
    """给插件扫描到的一批表单字段算出填表计划。

    light_client 允许没配置（返回 None）：这时只有关键词能直接命中的字段
    会被填,需要 LLM 兜底判断的字段全部标记为 unmapped——不能因为用户还没
    配置模型就让这个功能完全不可用,退化成"只能覆盖最常见的固定字段"是
    可以接受的降级,而不是报错拒绝整个请求。
    """
    fields = [
        {
            "field_id": f.field_id,
            "label": f.label,
            "input_type": f.input_type,
            "options": [o.model_dump() for o in f.options],
        }
        for f in payload.fields
    ]
    try:
        actions = build_autofill_plan(db, fields, light_client, jd_id=payload.jd_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"生成填表计划失败: {exc}") from exc

    return ExtensionAutofillPlanResponse(
        plan=[
            ExtensionAutofillAction(
                field_id=a.field_id,
                action=a.action,
                value=a.value,
                source=a.source,
                reason=a.reason,
                similarity=a.similarity,
            )
            for a in actions
        ]
    )


class ExtensionQASaveRequest(BaseModel):
    question_text: str
    answer_text: str
    jd_id: int | None = None


class ExtensionQASaveResponse(BaseModel):
    saved: bool


@router.post("/autofill/qa-save", response_model=ExtensionQASaveResponse)
def autofill_qa_save(payload: ExtensionQASaveRequest, db: Session = Depends(get_db)) -> ExtensionQASaveResponse:
    """用户在投递表单里手打了一道题库里没有的问答题之后，插件把这道题连同
    答案回存进 qa_bank（source=APPLICATION，标记是"投递过程中新遇到并
    补充"而不是建画像阶段主动收集的），下次遇到相似的问题就能被
    `find_similar_answer` 检索到直接复用，题库会随着真实投递逐渐养大。"""
    entry = add_qa_entry(
        db,
        question_text=payload.question_text,
        answer_text=payload.answer_text,
        source=QASource.APPLICATION,
        jd_id=payload.jd_id,
    )
    return ExtensionQASaveResponse(saved=entry is not None)


@router.get("/autofill/resume-pdf/{jd_id}")
def autofill_resume_pdf(jd_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """给这条 JD 最近生成的一份简历 PDF，供插件把它注入到 ATS 的简历上传
    控件里（`DataTransfer` 往 file input 塞文件，插件侧实现）。取"最近生成
    的一份"而不是让插件指定 resume_id：插件不需要、也不应该关心简历重制
    模块内部 K 值/风格版本这些细节，只需要"这条 JD 目前最新的简历"。"""
    jd = db.get(JDRecord, jd_id)
    if jd is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="JD 不存在")

    resume_version = (
        db.query(ResumeVersion)
        .filter(ResumeVersion.jd_id == jd_id)
        .order_by(ResumeVersion.id.desc())
        .first()
    )
    if resume_version is None or not resume_version.pdf_path or not Path(resume_version.pdf_path).exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="这条 JD 还没有生成过简历 PDF")

    filename = f"resume_{jd.company or 'jobpilot'}_{resume_version.k_value}.pdf".replace(" ", "_")
    return FileResponse(resume_version.pdf_path, media_type="application/pdf", filename=filename)


class ExtensionMarkAppliedResponse(BaseModel):
    jd_id: int
    status: str


@router.post("/jobs/{jd_id}/mark-applied", response_model=ExtensionMarkAppliedResponse)
def mark_job_applied(jd_id: int, db: Session = Depends(get_db)) -> ExtensionMarkAppliedResponse:
    """投递页提交按钮被点击、且能确认这次点击对应的是哪条 JD（见插件侧
    dashboard_bridge 的"去投递"关联机制）时，把这条 JD 的状态推进成
    APPLIED。这里不做状态机顺序校验（比如要求必须先是 TAILORED 才能变
    APPLIED）——用户完全可能跳过简历重制直接投递,状态机的意义是"记录
    进度"，不是"强制流程"，不应该因为用户没有走"标准路径"就拒绝更新。"""
    jd = db.get(JDRecord, jd_id)
    if jd is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="JD 不存在")
    jd.status = JDStatus.APPLIED
    db.commit()
    return ExtensionMarkAppliedResponse(jd_id=jd.id, status=jd.status.value)
