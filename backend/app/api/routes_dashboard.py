"""
本地 Dashboard：直接在用户自己的浏览器里打开，不经过插件。
GET 路由不做鉴权（只读、且只监听 127.0.0.1）；POST 路由用 require_local_browser
做最基本的跨站请求防护（见 app/api/deps.py 的说明）。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import require_local_browser
from app.api.deps_llm import get_heavy_client, get_light_client
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.llm_client import LLMClient
from app.core.paths import app_root
from app.core.secrets import set_secret
from app.models.tables import (
    ExperienceEntry,
    ExperienceLevel,
    JDRecord,
    JDStatus,
    LLMUsageLog,
    MatchScore,
    ModelConfig,
    ModelSlot,
    ResumeVersion,
)
from app.services.analysis import JDNotFoundError, analyze_jd
from app.services.jd_ingest import create_jd
from app.services.profile_deepening import (
    backfill_bullet_triads,
    generate_background_questions,
    merge_background_answers,
)
from app.services.profile_service import (
    add_bullet,
    add_company,
    add_education_entry,
    add_personal_project,
    add_personal_project_bullet,
    add_position,
    delete_bullet,
    delete_company,
    delete_education_entry,
    delete_personal_project,
    delete_personal_project_bullet,
    delete_position,
    get_education_entries,
    get_experience_tree,
    get_or_create_profile_basic,
    get_personal_projects,
    merge_parsed_experience,
    resolve_bullet_conflict,
    resolve_position_field,
    update_bullet_content,
    update_education_entry,
    update_personal_project,
    update_position_fields,
    update_profile_basic,
)
from app.services.qa_bank_service import (
    delete_qa_entry,
    generate_common_qa_questions,
    list_qa_entries,
    save_qa_answers,
)
from app.services.resume_ingest import extract_and_structure, extract_text
from app.services.resume_md_parser import parse_resume_markdown
from app.services.resume_pdf import AVAILABLE_RESUME_STYLES, MD_TEMPLATE_STYLE_SENTINEL, UnknownResumeStyleError
from app.services.resume_tailor import (
    JDNotFoundError as TailorJDNotFoundError,
    ResumeVersionNotFoundError,
    build_resume_draft,
    confirm_and_finalize,
    regenerate_resume_pdf,
)
from app.services.resume_template_service import (
    ResumeTemplateNotFoundError,
    ResumeTemplateRenderError,
    create_template as create_resume_template,
    delete_template as delete_resume_template,
    get_or_create_default_template,
    list_templates as list_resume_templates,
    set_default_template,
    update_template_content,
)

router = APIRouter(prefix="/dashboard")

TEMPLATES_DIR = app_root() / "app" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

STATUS_LABELS = {
    JDStatus.PENDING.value: "未分析",
    JDStatus.ANALYZING.value: "正在分析",
    JDStatus.ANALYZED.value: "已分析",
    JDStatus.TAILORED.value: "已定制简历",
    JDStatus.APPLIED.value: "已投递",
}


def _flash_params(request: Request) -> dict:
    flash = request.query_params.get("flash")
    return {
        "flash": unquote(flash) if flash else None,
        "flash_error": request.query_params.get("error") == "1",
    }


def _redirect_with_flash(url: str, message: str, error: bool = False) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    suffix = f"{sep}flash={quote(message)}"
    if error:
        suffix += "&error=1"
    return RedirectResponse(url=url + suffix, status_code=303)


@router.get("/", include_in_schema=False)
def dashboard_root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard/jobs")


# ---------- 画像 ----------


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    profile = get_or_create_profile_basic(db)
    experience = get_experience_tree(db)
    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "active": "profile",
            "profile": profile,
            "experience": experience,
            "education_entries": get_education_entries(db),
            "personal_projects": get_personal_projects(db),
            **_flash_params(request),
        },
    )


@router.post("/profile", dependencies=[Depends(require_local_browser)])
def profile_update(
    request: Request,
    db: Session = Depends(get_db),
    full_name: str = Form(""),
    target_title: str = Form(""),
    education: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    linkedin_url: str = Form(""),
    github_url: str = Form(""),
    school: str = Form(""),
    years_experience: str = Form(""),
    current_location: str = Form(""),
    target_location: str = Form(""),
    work_authorization: str = Form(""),
    resume_summary: str = Form(""),
    skills_text: str = Form(""),
) -> RedirectResponse:
    fields = {
        "full_name": full_name or None,
        "target_title": target_title or None,
        "education": education or None,
        "phone": phone or None,
        "email": email or None,
        "linkedin_url": linkedin_url or None,
        "github_url": github_url or None,
        "school": school or None,
        "years_experience": float(years_experience) if years_experience else None,
        "current_location": current_location or None,
        "target_location": target_location or None,
        "work_authorization": work_authorization or None,
        "resume_summary": resume_summary or None,
        "skills_text": skills_text or None,
    }
    update_profile_basic(db, fields)
    return _redirect_with_flash("/dashboard/profile", "基本信息已保存")


@router.post("/profile/resume", dependencies=[Depends(require_local_browser)])
def profile_upload_resume(
    request: Request,
    db: Session = Depends(get_db),
    light_client: LLMClient | None = Depends(get_light_client),
    resume_file: UploadFile = None,
):
    if resume_file is None or not resume_file.filename:
        return _redirect_with_flash("/dashboard/profile", "没有选择文件", error=True)

    suffix = Path(resume_file.filename).suffix.lower()
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(resume_file.file.read())
        raw_text = extract_text(tmp_path)
        # .md 简历如果符合 JobPilot 模板约定，规则解析完全不需要 LLM；只有
        # 解析不出来（非 md，或者不认识的自由格式 md）才会真的用到
        # light_client，所以"轻量模型还没配置"这个检查放在这里、而不是
        # 一进来就无条件拦截——不能让"还没配模型"挡住本来完全不需要模型
        # 的规则解析路径。
        rule_parsed = parse_resume_markdown(raw_text) if suffix == ".md" else None
        if rule_parsed is not None:
            parsed = rule_parsed
        else:
            if light_client is None:
                return _redirect_with_flash(
                    "/dashboard/profile", "轻量模型还没配置，请先到模型配置页面填写", error=True
                )
            parsed = extract_and_structure(tmp_path, raw_text, light_client)
        result = merge_parsed_experience(db, parsed)
        # 新增/更新的 bullet 顺带做一次关键词/行为/结果三元组抽取，供后面
        # 简历重制阶段做关键词匹配用。抽取失败不应该让整个上传流程失败——
        # 三元组只是衍生索引，没有它简历重制仍然能跑，只是匹配会更粗。
        # light_client 为 None（规则解析成功、用户还没配模型）时直接跳过，
        # 不强行报错——三元组抽取本来就是可选的衍生步骤。
        triads_backfilled = 0
        if light_client is not None:
            try:
                triads_backfilled = backfill_bullet_triads(db, light_client)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 - 面向用户的友好提示，细节已经包含在异常信息里
        return _redirect_with_flash("/dashboard/profile", f"简历解析失败: {exc}", error=True)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    extra_bits = []
    if "resume_summary" in result.basic_fields_filled:
        extra_bits.append("个人总结")
    if "skills_text" in result.basic_fields_filled:
        extra_bits.append("技能")
    if result.education_added:
        extra_bits.append(f"{result.education_added} 条教育经历")
    if result.projects_added or result.project_bullets_added:
        extra_bits.append(f"{result.projects_added} 个独立项目（{result.project_bullets_added} 条贡献句）")
    extra_msg = f"，另外补充了{'、'.join(extra_bits)}" if extra_bits else ""

    msg = (
        f"解析完成：新增 {result.companies_added} 家公司、{result.positions_added} 段经历、"
        f"{result.bullets_added} 条贡献句（跳过 {result.bullets_skipped_duplicate} 条重复），"
        f"已为其中 {triads_backfilled} 条贡献句提炼关键词{extra_msg}"
    )

    if result.bullet_conflicts or result.position_field_conflicts:
        # 有需要用户确认的冲突（近似重复的贡献句 / 职位字段和已有记录不一致）：
        # 自动合并的部分已经生效了，冲突的部分先展示出来让用户逐条选择，
        # 不在这里直接跳转回画像页。
        return templates.TemplateResponse(
            "merge_conflicts.html",
            {
                "request": request,
                "active": "profile",
                "summary": msg,
                "bullet_conflicts": result.bullet_conflicts,
                "position_field_conflicts": result.position_field_conflicts,
            },
        )
    return _redirect_with_flash("/dashboard/profile", msg)


@router.post("/profile/merge-conflicts/resolve", dependencies=[Depends(require_local_browser)])
async def profile_merge_conflicts_resolve(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    count = int(form.get("conflict_count", "0") or "0")
    applied = 0
    for i in range(count):
        kind = form.get(f"kind_{i}")
        choice = form.get(f"choice_{i}", "keep_old")
        position_id = int(form.get(f"position_id_{i}", "0") or "0")
        if kind == "bullet":
            raw_bullet_id = form.get(f"existing_bullet_id_{i}", "")
            existing_bullet_id = int(raw_bullet_id) if raw_bullet_id else None
            new_text = form.get(f"new_text_{i}", "")
            if choice != "keep_old":
                resolve_bullet_conflict(db, position_id, choice, existing_bullet_id, new_text)
                applied += 1
        elif kind == "position_field":
            field_name = form.get(f"field_{i}", "")
            new_value = form.get(f"new_value_{i}", "")
            if choice == "use_new":
                resolve_position_field(db, position_id, field_name, new_value)
                applied += 1
    return _redirect_with_flash("/dashboard/profile", f"已处理 {count} 条冲突（其中 {applied} 条采用了新内容）")


# ---------- 工作经历树的手动增删改 ----------


@router.post("/profile/companies", dependencies=[Depends(require_local_browser)])
def profile_add_company(db: Session = Depends(get_db), company_name: str = Form(...)) -> RedirectResponse:
    try:
        add_company(db, company_name)
    except ValueError as exc:
        return _redirect_with_flash("/dashboard/profile", str(exc), error=True)
    return _redirect_with_flash("/dashboard/profile", "已新增公司")


@router.post("/profile/companies/{company_id}/delete", dependencies=[Depends(require_local_browser)])
def profile_delete_company(company_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    delete_company(db, company_id)
    return _redirect_with_flash("/dashboard/profile", "已删除公司及其下所有经历")


@router.post("/profile/companies/{company_id}/positions", dependencies=[Depends(require_local_browser)])
def profile_add_position(
    company_id: int,
    db: Session = Depends(get_db),
    position_title: str = Form(""),
    project_name: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    is_current: str = Form(""),
) -> RedirectResponse:
    try:
        position = add_position(
            db,
            company_id,
            position_title=position_title,
            project_name=project_name,
            start_date=start_date,
            end_date=end_date,
            is_current=bool(is_current),
        )
    except ValueError as exc:
        return _redirect_with_flash("/dashboard/profile", str(exc), error=True)
    return _redirect_with_flash(f"/dashboard/profile/positions/{position.id}", "已新增职位")


@router.post("/profile/positions/{position_id}/update", dependencies=[Depends(require_local_browser)])
def profile_update_position(
    position_id: int,
    db: Session = Depends(get_db),
    position_title: str = Form(""),
    project_name: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    is_current: str = Form(""),
) -> RedirectResponse:
    detail_url = f"/dashboard/profile/positions/{position_id}"
    try:
        update_position_fields(
            db,
            position_id,
            {
                "position_title": position_title,
                "project_name": project_name,
                "start_date": start_date,
                "end_date": end_date,
                "is_current": bool(is_current),
            },
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="position not found")
    return _redirect_with_flash(detail_url, "已更新")


@router.post("/profile/positions/{position_id}/delete", dependencies=[Depends(require_local_browser)])
def profile_delete_position(position_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    delete_position(db, position_id)
    return _redirect_with_flash("/dashboard/profile", "已删除该段职位经历")


@router.post("/profile/positions/{position_id}/bullets", dependencies=[Depends(require_local_browser)])
def profile_add_bullet(position_id: int, db: Session = Depends(get_db), content: str = Form(...)) -> RedirectResponse:
    detail_url = f"/dashboard/profile/positions/{position_id}"
    try:
        add_bullet(db, position_id, content)
    except ValueError as exc:
        return _redirect_with_flash(detail_url, str(exc), error=True)
    return _redirect_with_flash(detail_url, "已新增贡献句")


@router.post("/profile/bullets/{bullet_id}/update", dependencies=[Depends(require_local_browser)])
def profile_update_bullet(
    bullet_id: int, db: Session = Depends(get_db), content: str = Form(...), position_id: int = Form(...)
) -> RedirectResponse:
    detail_url = f"/dashboard/profile/positions/{position_id}"
    try:
        update_bullet_content(db, bullet_id, content)
    except ValueError as exc:
        return _redirect_with_flash(detail_url, str(exc), error=True)
    return _redirect_with_flash(detail_url, "已更新贡献句")


@router.post("/profile/bullets/{bullet_id}/delete", dependencies=[Depends(require_local_browser)])
def profile_delete_bullet(bullet_id: int, position_id: int = Form(...), db: Session = Depends(get_db)) -> RedirectResponse:
    delete_bullet(db, bullet_id)
    return _redirect_with_flash(f"/dashboard/profile/positions/{position_id}", "已删除贡献句")


# ---------- 教育经历 / 独立项目（打磨阶段后新增，见 profile.html 的对应板块） ----------


@router.post("/profile/education", dependencies=[Depends(require_local_browser)])
def profile_add_education(
    db: Session = Depends(get_db),
    school: str = Form(...),
    degree: str = Form(""),
    location: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    is_current: str = Form(""),
) -> RedirectResponse:
    try:
        add_education_entry(
            db, school, degree=degree, location=location, start_date=start_date, end_date=end_date, is_current=bool(is_current)
        )
    except ValueError as exc:
        return _redirect_with_flash("/dashboard/profile", str(exc), error=True)
    return _redirect_with_flash("/dashboard/profile", "已新增教育经历")


@router.post("/profile/education/{entry_id}/update", dependencies=[Depends(require_local_browser)])
def profile_update_education(
    entry_id: int,
    db: Session = Depends(get_db),
    school: str = Form(""),
    degree: str = Form(""),
    location: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    is_current: str = Form(""),
) -> RedirectResponse:
    try:
        update_education_entry(
            db,
            entry_id,
            {
                "school": school,
                "degree": degree,
                "location": location,
                "start_date": start_date,
                "end_date": end_date,
                "is_current": bool(is_current),
            },
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="education entry not found")
    return _redirect_with_flash("/dashboard/profile", "已更新教育经历")


@router.post("/profile/education/{entry_id}/delete", dependencies=[Depends(require_local_browser)])
def profile_delete_education(entry_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    delete_education_entry(db, entry_id)
    return _redirect_with_flash("/dashboard/profile", "已删除教育经历")


@router.post("/profile/projects", dependencies=[Depends(require_local_browser)])
def profile_add_project(
    db: Session = Depends(get_db),
    project_name: str = Form(...),
    start_date: str = Form(""),
    end_date: str = Form(""),
    is_current: str = Form(""),
) -> RedirectResponse:
    try:
        add_personal_project(db, project_name, start_date=start_date, end_date=end_date, is_current=bool(is_current))
    except ValueError as exc:
        return _redirect_with_flash("/dashboard/profile", str(exc), error=True)
    return _redirect_with_flash("/dashboard/profile", "已新增独立项目")


@router.post("/profile/projects/{project_id}/update", dependencies=[Depends(require_local_browser)])
def profile_update_project(
    project_id: int,
    db: Session = Depends(get_db),
    project_name: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    is_current: str = Form(""),
) -> RedirectResponse:
    try:
        update_personal_project(
            db, project_id, {"project_name": project_name, "start_date": start_date, "end_date": end_date, "is_current": bool(is_current)}
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="project not found")
    return _redirect_with_flash("/dashboard/profile", "已更新独立项目")


@router.post("/profile/projects/{project_id}/delete", dependencies=[Depends(require_local_browser)])
def profile_delete_project(project_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    delete_personal_project(db, project_id)
    return _redirect_with_flash("/dashboard/profile", "已删除独立项目")


@router.post("/profile/projects/{project_id}/bullets", dependencies=[Depends(require_local_browser)])
def profile_add_project_bullet(project_id: int, db: Session = Depends(get_db), content: str = Form(...)) -> RedirectResponse:
    try:
        add_personal_project_bullet(db, project_id, content)
    except ValueError as exc:
        return _redirect_with_flash("/dashboard/profile", str(exc), error=True)
    return _redirect_with_flash("/dashboard/profile", "已新增项目贡献句")


@router.post("/profile/project-bullets/{bullet_id}/delete", dependencies=[Depends(require_local_browser)])
def profile_delete_project_bullet(bullet_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    delete_personal_project_bullet(db, bullet_id)
    return _redirect_with_flash("/dashboard/profile", "已删除项目贡献句")


# ---------- MD 简历模板库（打磨阶段后新增） ----------


@router.get("/resume-templates", response_class=HTMLResponse)
def resume_templates_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    get_or_create_default_template(db)  # 确保空库场景下也有至少一个能选的模板
    return templates.TemplateResponse(
        "resume_templates.html",
        {
            "request": request,
            "active": "resume_templates",
            "resume_templates": list_resume_templates(db),
            **_flash_params(request),
        },
    )


@router.post("/resume-templates", dependencies=[Depends(require_local_browser)])
def resume_template_create(
    db: Session = Depends(get_db),
    name: str = Form(...),
    content: str = Form(...),
    set_default: str = Form(""),
) -> RedirectResponse:
    try:
        create_resume_template(db, name, content, set_default=bool(set_default))
    except (ValueError, ResumeTemplateRenderError) as exc:
        return _redirect_with_flash("/dashboard/resume-templates", str(exc), error=True)
    return _redirect_with_flash("/dashboard/resume-templates", "已新增模板")


@router.post("/resume-templates/{template_id}/update", dependencies=[Depends(require_local_browser)])
def resume_template_update(
    template_id: int, db: Session = Depends(get_db), name: str = Form(""), content: str = Form("")
) -> RedirectResponse:
    try:
        update_template_content(db, template_id, name=name or None, content=content or None)
    except ResumeTemplateNotFoundError:
        raise HTTPException(status_code=404, detail="template not found")
    except (ValueError, ResumeTemplateRenderError) as exc:
        return _redirect_with_flash("/dashboard/resume-templates", str(exc), error=True)
    return _redirect_with_flash("/dashboard/resume-templates", "模板已更新")


@router.post("/resume-templates/{template_id}/set-default", dependencies=[Depends(require_local_browser)])
def resume_template_set_default(template_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        set_default_template(db, template_id)
    except ResumeTemplateNotFoundError:
        raise HTTPException(status_code=404, detail="template not found")
    return _redirect_with_flash("/dashboard/resume-templates", "已设为默认模板")


@router.post("/resume-templates/{template_id}/delete", dependencies=[Depends(require_local_browser)])
def resume_template_delete(template_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        delete_resume_template(db, template_id)
    except ResumeTemplateNotFoundError:
        raise HTTPException(status_code=404, detail="template not found")
    except ValueError as exc:
        return _redirect_with_flash("/dashboard/resume-templates", str(exc), error=True)
    return _redirect_with_flash("/dashboard/resume-templates", "已删除模板")


# ---------- JD 列表 / 详情 ----------


@router.get("/jobs", response_class=HTMLResponse)
def jobs_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    jds = db.query(JDRecord).order_by(JDRecord.created_at.desc()).all()
    rows = []
    for jd in jds:
        latest_score = (
            db.query(MatchScore)
            .filter(MatchScore.jd_id == jd.id)
            .order_by(MatchScore.created_at.desc())
            .first()
        )
        rows.append((jd, latest_score))
    return templates.TemplateResponse(
        "jobs_list.html",
        {
            "request": request,
            "active": "jobs",
            "jobs": rows,
            "status_labels": STATUS_LABELS,
            **_flash_params(request),
        },
    )


@router.get("/jobs/new", response_class=HTMLResponse)
def job_new_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "job_new.html", {"request": request, "active": "jobs", **_flash_params(request)}
    )


@router.post("/jobs", dependencies=[Depends(require_local_browser)])
def job_create(
    db: Session = Depends(get_db),
    company: str = Form(""),
    title: str = Form(""),
    location: str = Form(""),
    source_url: str = Form(""),
    description_raw: str = Form(...),
    extra_meta_raw: str = Form(""),
) -> RedirectResponse:
    jd = create_jd(
        db,
        company=company or None,
        title=title or None,
        description_raw=description_raw,
        location=location or None,
        source_url=source_url or None,
        extra_meta_raw=extra_meta_raw or None,
    )
    return _redirect_with_flash(f"/dashboard/jobs/{jd.id}", "JD 已保存")


@router.get("/jobs/{jd_id}", response_class=HTMLResponse)
def job_detail(jd_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    jd = db.get(JDRecord, jd_id)
    if jd is None:
        raise HTTPException(status_code=404, detail="JD not found")
    latest_score = (
        db.query(MatchScore).filter(MatchScore.jd_id == jd.id).order_by(MatchScore.created_at.desc()).first()
    )
    parsed_meta_pretty = json.dumps(jd.parsed_meta, ensure_ascii=False, indent=2) if jd.parsed_meta else None
    return templates.TemplateResponse(
        "job_detail.html",
        {
            "request": request,
            "active": "jobs",
            "jd": jd,
            "score": latest_score,
            "status_labels": STATUS_LABELS,
            "parsed_meta_pretty": parsed_meta_pretty,
            **_flash_params(request),
        },
    )


@router.post("/jobs/{jd_id}/analyze", dependencies=[Depends(require_local_browser)])
def job_analyze(
    jd_id: int,
    db: Session = Depends(get_db),
    light_client: LLMClient | None = Depends(get_light_client),
    heavy_client: LLMClient | None = Depends(get_heavy_client),
) -> RedirectResponse:
    if light_client is None or heavy_client is None:
        missing = "轻量" if light_client is None else "重量"
        return _redirect_with_flash(
            f"/dashboard/jobs/{jd_id}", f"{missing}模型还没配置，请先到模型配置页面填写", error=True
        )
    try:
        analyze_jd(db, jd_id, light_client, heavy_client)
    except JDNotFoundError:
        raise HTTPException(status_code=404, detail="JD not found")
    except Exception as exc:  # noqa: BLE001
        return _redirect_with_flash(f"/dashboard/jobs/{jd_id}", f"分析失败: {exc}", error=True)
    return _redirect_with_flash(f"/dashboard/jobs/{jd_id}", "分析完成")


@router.post("/jobs/{jd_id}/mark-applied", dependencies=[Depends(require_local_browser)])
def job_mark_applied(jd_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    """Phase 4：手动兜底入口。插件那一套"去投递"关联 + 提交按钮监听（见
    `app/api/routes_extension.py` 的同名接口）是尽力而为的自动化,不保证
    100% 覆盖所有情况（比如某家 ATS 的提交按钮文案不在插件识别的关键词
    模式里,或者用户压根没有安装插件),这里补一个手动按钮兜底,用户随时
    可以自己在 Dashboard 上把状态标记成"已投递",不依赖插件是否成功监听到。"""
    jd = db.get(JDRecord, jd_id)
    if jd is None:
        raise HTTPException(status_code=404, detail="JD not found")
    jd.status = JDStatus.APPLIED
    db.commit()
    return _redirect_with_flash(f"/dashboard/jobs/{jd_id}", "已标记为已投递")


# ---------- 模型配置 ----------


@router.get("/models", response_class=HTMLResponse)
def models_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    configs = {}
    for slot in ModelSlot:
        row = db.query(ModelConfig).filter(ModelConfig.slot == slot).one_or_none()
        configs[slot.value] = {
            "base_url": row.base_url if row else None,
            "model_name": row.model_name if row else None,
            "has_key": bool(row and row.keyring_ref),
        }
    return templates.TemplateResponse(
        "models.html",
        {"request": request, "active": "models", "configs": configs, **_flash_params(request)},
    )


@router.get("/usage", response_class=HTMLResponse)
def usage_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Phase 5"用量统计面板"：按槽位汇总调用次数/成功失败次数/token 总数，
    再列最近 50 条明细。数据来自 `app.core.llm_factory.build_client` 包的
    `UsageTrackingLLMClient`，只要是通过真实配置调用的模型（不是测试里
    `dependency_overrides` 直接换上去的 `FakeLLMClient`）都会被记下来。"""
    summary = {}
    for slot in ModelSlot:
        total_calls = db.query(LLMUsageLog).filter(LLMUsageLog.slot == slot).count()
        ok_calls = (
            db.query(LLMUsageLog).filter(LLMUsageLog.slot == slot, LLMUsageLog.ok.is_(True)).count()
        )
        total_tokens = (
            db.query(func.coalesce(func.sum(LLMUsageLog.total_tokens), 0))
            .filter(LLMUsageLog.slot == slot)
            .scalar()
        )
        summary[slot.value] = {
            "total_calls": total_calls,
            "ok_calls": ok_calls,
            "failed_calls": total_calls - ok_calls,
            "total_tokens": total_tokens or 0,
        }
    recent_logs = (
        db.query(LLMUsageLog).order_by(LLMUsageLog.created_at.desc(), LLMUsageLog.id.desc()).limit(50).all()
    )
    return templates.TemplateResponse(
        "usage.html",
        {
            "request": request,
            "active": "usage",
            "summary": summary,
            "recent_logs": recent_logs,
            **_flash_params(request),
        },
    )


@router.post("/models/{slot}", dependencies=[Depends(require_local_browser)])
def models_update(
    slot: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    base_url: str = Form(""),
    model_name: str = Form(""),
    api_key: str = Form(""),
) -> RedirectResponse:
    try:
        slot_enum = ModelSlot(slot)
    except ValueError:
        raise HTTPException(status_code=404, detail="unknown slot")

    row = db.query(ModelConfig).filter(ModelConfig.slot == slot_enum).one_or_none()
    if row is None:
        row = ModelConfig(slot=slot_enum)
        db.add(row)

    row.base_url = base_url or None
    row.model_name = model_name or None
    if api_key:
        ref = f"model_config:{slot}:api_key"
        set_secret(settings, ref, api_key)
        row.keyring_ref = ref
    db.commit()
    return _redirect_with_flash("/dashboard/models", f"{slot} 配置已保存")


# ---------- 项目背景深化（Phase 2：追问式访谈） ----------


def _get_position_or_404(db: Session, position_id: int) -> ExperienceEntry:
    position = db.get(ExperienceEntry, position_id)
    if position is None or position.level != ExperienceLevel.POSITION:
        raise HTTPException(status_code=404, detail="position not found")
    return position


@router.get("/profile/positions/{position_id}", response_class=HTMLResponse)
def position_detail(position_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    position = _get_position_or_404(db, position_id)
    return templates.TemplateResponse(
        "position_detail.html",
        {
            "request": request,
            "active": "profile",
            "position": position,
            "company_name": position.parent.company_name if position.parent else None,
            **_flash_params(request),
        },
    )


@router.get("/profile/positions/{position_id}/interview", response_class=HTMLResponse)
def position_interview_start(
    position_id: int,
    request: Request,
    db: Session = Depends(get_db),
    light_client: LLMClient | None = Depends(get_light_client),
):
    position = _get_position_or_404(db, position_id)
    detail_url = f"/dashboard/profile/positions/{position_id}"
    if light_client is None:
        return _redirect_with_flash(detail_url, "轻量模型还没配置，请先到模型配置页面填写", error=True)
    try:
        questions = generate_background_questions(position, light_client)
    except Exception as exc:  # noqa: BLE001
        return _redirect_with_flash(detail_url, f"生成访谈问题失败: {exc}", error=True)
    if not questions:
        return _redirect_with_flash(detail_url, "当前信息已经比较完整，系统这一轮没有新问题要问")
    return templates.TemplateResponse(
        "position_interview.html",
        {
            "request": request,
            "active": "profile",
            "position": position,
            "questions": questions,
            **_flash_params(request),
        },
    )


@router.post("/profile/positions/{position_id}/interview", dependencies=[Depends(require_local_browser)])
async def position_interview_submit(
    position_id: int,
    request: Request,
    db: Session = Depends(get_db),
    light_client: LLMClient | None = Depends(get_light_client),
) -> RedirectResponse:
    _get_position_or_404(db, position_id)
    detail_url = f"/dashboard/profile/positions/{position_id}"
    if light_client is None:
        return _redirect_with_flash(detail_url, "轻量模型还没配置，请先到模型配置页面填写", error=True)

    form = await request.form()
    question_count = int(form.get("question_count", "0") or "0")
    qa_pairs = [
        {"question": form.get(f"question_{i}", ""), "answer": form.get(f"answer_{i}", "")}
        for i in range(question_count)
    ]
    try:
        merge_background_answers(db, position_id, qa_pairs, light_client)
    except ValueError:
        raise HTTPException(status_code=404, detail="position not found")
    except Exception as exc:  # noqa: BLE001
        return _redirect_with_flash(detail_url, f"整理项目背景描述失败: {exc}", error=True)
    return _redirect_with_flash(detail_url, "项目背景描述已更新")


# ---------- 简历重制（Phase 2：K 值驱动 + 技能延伸建议确认） ----------


def _parse_render_choice(raw: str | None) -> tuple[str, int | None]:
    """生成/重新生成简历页面上的"风格"下拉框，内置风格和 MD 模板混在同一个
    下拉框里选，选项 value 用一个前缀区分（"style:default" / "template:3"），
    这里统一解析成 (style_id, resume_template_id) 二元组——两个下游函数
    （confirm_and_finalize/regenerate_resume_pdf）都是这个约定，resume_template_id
    非 None 时 style_id 会被忽略。

    这里只负责拆前缀，不校验 style_id 是不是一个真实存在的内置风格——
    "生成"和"重新生成"这两个调用方对"选了个不存在的风格"要不要报错的
    容忍度不一样（前者悄悄回退成 default，后者要明确报错，见各自调用处
    的注释），校验策略留给调用方决定，不要在这个共享的解析函数里就定死。
    解析不出模板 id（表单被篡改）时兜底成内置默认风格。"""
    raw = (raw or "").strip()
    if raw.startswith("template:"):
        try:
            return "default", int(raw.split(":", 1)[1])
        except ValueError:
            return "default", None
    if raw.startswith("style:"):
        return raw.split(":", 1)[1] or "default", None
    return "default", None


@router.get("/jobs/{jd_id}/tailor", response_class=HTMLResponse)
def job_tailor_draft(
    jd_id: int,
    request: Request,
    db: Session = Depends(get_db),
    light_client: LLMClient | None = Depends(get_light_client),
    heavy_client: LLMClient | None = Depends(get_heavy_client),
    k: int = 5,
):
    jd = db.get(JDRecord, jd_id)
    if jd is None:
        raise HTTPException(status_code=404, detail="JD not found")
    detail_url = f"/dashboard/jobs/{jd_id}"
    if light_client is None or heavy_client is None:
        missing = "轻量" if light_client is None else "重量"
        return _redirect_with_flash(detail_url, f"{missing}模型还没配置，请先到模型配置页面填写", error=True)

    k_value = max(0, min(10, k))
    try:
        draft = build_resume_draft(db, jd_id, k_value, light_client, heavy_client)
    except TailorJDNotFoundError:
        raise HTTPException(status_code=404, detail="JD not found")
    except Exception as exc:  # noqa: BLE001
        return _redirect_with_flash(detail_url, f"生成简历草稿失败: {exc}", error=True)

    return templates.TemplateResponse(
        "resume_tailor.html",
        {
            "request": request,
            "active": "jobs",
            "jd": jd,
            "k_value": k_value,
            "hit_items": draft.hit_items,
            "suggestions": draft.suggestions,
            "hit_items_json": json.dumps(draft.hit_items, ensure_ascii=False),
            "available_styles": AVAILABLE_RESUME_STYLES,
            "resume_templates": list_resume_templates(db),
            **_flash_params(request),
        },
    )


@router.post("/jobs/{jd_id}/tailor/confirm", dependencies=[Depends(require_local_browser)])
async def job_tailor_confirm(jd_id: int, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    detail_url = f"/dashboard/jobs/{jd_id}"
    form = await request.form()
    k_value = int(form.get("k_value", "0") or "0")
    try:
        hit_items = json.loads(form.get("hit_items_json") or "[]")
    except json.JSONDecodeError:
        hit_items = []

    suggestion_count = int(form.get("suggestion_count", "0") or "0")
    accepted_suggestions = []
    for i in range(suggestion_count):
        if form.get(f"accept_{i}") != "on":
            continue
        entry_id_raw = form.get(f"entry_{i}")
        if not entry_id_raw:
            continue
        accepted_suggestions.append(
            {
                "keyword": form.get(f"keyword_{i}", ""),
                "experience_entry_id": int(entry_id_raw),
                "action_summary": form.get(f"action_{i}", ""),
                "result_summary": form.get(f"result_{i}") or None,
                "rationale": form.get(f"rationale_{i}", ""),
            }
        )

    style_id, resume_template_id = _parse_render_choice(form.get("render_choice"))
    # 挑错风格不应该让整个"生成简历"操作失败，大不了渲染出来的是默认风格
    # （MD 模板路径不受影响——resume_template_id 非空时 style_id 本来就会
    # 被 confirm_and_finalize 忽略）。
    if resume_template_id is None and style_id not in AVAILABLE_RESUME_STYLES:
        style_id = "default"

    try:
        resume_version = confirm_and_finalize(
            db, jd_id, k_value, hit_items, accepted_suggestions, style_id, resume_template_id
        )
    except TailorJDNotFoundError:
        raise HTTPException(status_code=404, detail="JD not found")
    except ResumeTemplateNotFoundError:
        return _redirect_with_flash(detail_url, "选中的 MD 模板不存在了，请重新选择", error=True)
    except Exception as exc:  # noqa: BLE001
        return _redirect_with_flash(detail_url, f"生成简历失败: {exc}", error=True)

    return _redirect_with_flash(f"{detail_url}/resumes/{resume_version.id}", "简历已生成")


@router.get("/jobs/{jd_id}/resumes/{resume_id}", response_class=HTMLResponse)
def resume_version_detail(
    jd_id: int, resume_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    jd = db.get(JDRecord, jd_id)
    resume_version = db.get(ResumeVersion, resume_id)
    if jd is None or resume_version is None or resume_version.jd_id != jd_id:
        raise HTTPException(status_code=404, detail="resume version not found")
    return templates.TemplateResponse(
        "resume_result.html",
        {
            "request": request,
            "active": "jobs",
            "jd": jd,
            "resume_version": resume_version,
            "available_styles": AVAILABLE_RESUME_STYLES,
            "resume_templates": list_resume_templates(db),
            "md_template_style_sentinel": MD_TEMPLATE_STYLE_SENTINEL,
            **_flash_params(request),
        },
    )


@router.post("/jobs/{jd_id}/resumes/{resume_id}/regenerate-pdf", dependencies=[Depends(require_local_browser)])
def resume_version_regenerate_pdf(
    jd_id: int, resume_id: int, db: Session = Depends(get_db), render_choice: str = Form("style:default")
) -> RedirectResponse:
    """Phase 5：简历风格自定义——不重新走 LLM 生成，只用已经存好的
    `resume_json` 换一套风格/MD 模板重新渲染，方便用户随便切换对比效果。"""
    result_url = f"/dashboard/jobs/{jd_id}/resumes/{resume_id}"
    style_id, resume_template_id = _parse_render_choice(render_choice)
    try:
        regenerate_resume_pdf(db, resume_id, style_id, resume_template_id)
    except ResumeVersionNotFoundError:
        raise HTTPException(status_code=404, detail="resume version not found")
    except ResumeTemplateNotFoundError:
        return _redirect_with_flash(result_url, "选中的 MD 模板不存在了，请重新选择", error=True)
    except UnknownResumeStyleError as exc:
        return _redirect_with_flash(result_url, str(exc), error=True)
    except Exception as exc:  # noqa: BLE001 - 例如 PdfRenderingUnavailableError / ResumeTemplateRenderError
        return _redirect_with_flash(result_url, f"重新生成失败: {exc}", error=True)
    return _redirect_with_flash(result_url, "已按新风格/模板重新生成")


@router.get("/jobs/{jd_id}/resumes/{resume_id}/pdf")
def resume_version_pdf(jd_id: int, resume_id: int, db: Session = Depends(get_db)) -> FileResponse:
    jd = db.get(JDRecord, jd_id)
    resume_version = db.get(ResumeVersion, resume_id)
    if jd is None or resume_version is None or resume_version.jd_id != jd_id:
        raise HTTPException(status_code=404, detail="resume version not found")
    if not resume_version.pdf_path or not Path(resume_version.pdf_path).exists():
        raise HTTPException(status_code=404, detail="PDF 还没有生成或已丢失，请重新生成简历")
    filename = f"resume_{jd.company or 'jobpilot'}_{resume_version.k_value}.pdf".replace(" ", "_")
    return FileResponse(resume_version.pdf_path, media_type="application/pdf", filename=filename)


# ---------- 题库（qa_bank）：Phase 2 补完第 4 项 ----------


@router.get("/qa-bank", response_class=HTMLResponse)
def qa_bank_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        "qa_bank.html",
        {
            "request": request,
            "active": "qa_bank",
            "entries": list_qa_entries(db),
            **_flash_params(request),
        },
    )


@router.post("/qa-bank", dependencies=[Depends(require_local_browser)])
def qa_bank_add(
    question_text: str = Form(...),
    answer_text: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    saved = save_qa_answers(db, [{"question": question_text, "answer": answer_text}])
    if saved:
        return _redirect_with_flash("/dashboard/qa-bank", "已保存")
    return _redirect_with_flash("/dashboard/qa-bank", "问题和答案都不能是空的", error=True)


@router.post("/qa-bank/{entry_id}/delete", dependencies=[Depends(require_local_browser)])
def qa_bank_delete(entry_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    delete_qa_entry(db, entry_id)
    return _redirect_with_flash("/dashboard/qa-bank", "已删除")


@router.get("/qa-bank/suggest", response_class=HTMLResponse)
def qa_bank_suggest(
    request: Request,
    db: Session = Depends(get_db),
    light_client: LLMClient | None = Depends(get_light_client),
):
    if light_client is None:
        return _redirect_with_flash("/dashboard/qa-bank", "轻量模型还没配置，请先到模型配置页面填写", error=True)
    profile = get_or_create_profile_basic(db)
    try:
        questions = generate_common_qa_questions(profile, light_client)
    except Exception as exc:  # noqa: BLE001
        return _redirect_with_flash("/dashboard/qa-bank", f"生成问题失败: {exc}", error=True)
    if not questions:
        return _redirect_with_flash("/dashboard/qa-bank", "这次没有生成出新的问题")
    return templates.TemplateResponse(
        "qa_bank_suggest.html",
        {
            "request": request,
            "active": "qa_bank",
            "questions": questions,
            **_flash_params(request),
        },
    )


@router.post("/qa-bank/suggest", dependencies=[Depends(require_local_browser)])
async def qa_bank_suggest_submit(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    question_count = int(form.get("question_count", "0") or "0")
    qa_pairs = [
        {"question": form.get(f"question_{i}", ""), "answer": form.get(f"answer_{i}", "")}
        for i in range(question_count)
    ]
    saved = save_qa_answers(db, qa_pairs)
    return _redirect_with_flash("/dashboard/qa-bank", f"已保存 {saved} 条回答")
