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
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.api.deps import require_local_browser
from app.api.deps_llm import get_heavy_client, get_light_client
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.llm_client import LLMClient
from app.core.secrets import set_secret
from app.models.tables import (
    ExperienceEntry,
    ExperienceLevel,
    JDRecord,
    JDStatus,
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
    get_experience_tree,
    get_or_create_profile_basic,
    merge_parsed_experience,
    update_profile_basic,
)
from app.services.resume_ingest import extract_text, structure_resume_text
from app.services.resume_tailor import (
    JDNotFoundError as TailorJDNotFoundError,
    build_resume_draft,
    confirm_and_finalize,
)

router = APIRouter(prefix="/dashboard")

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
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
    }
    update_profile_basic(db, fields)
    return _redirect_with_flash("/dashboard/profile", "基本信息已保存")


@router.post("/profile/resume", dependencies=[Depends(require_local_browser)])
def profile_upload_resume(
    db: Session = Depends(get_db),
    light_client: LLMClient | None = Depends(get_light_client),
    resume_file: UploadFile = None,
) -> RedirectResponse:
    if resume_file is None or not resume_file.filename:
        return _redirect_with_flash("/dashboard/profile", "没有选择文件", error=True)
    if light_client is None:
        return _redirect_with_flash(
            "/dashboard/profile", "轻量模型还没配置，请先到模型配置页面填写", error=True
        )

    suffix = Path(resume_file.filename).suffix.lower()
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(resume_file.file.read())
        raw_text = extract_text(tmp_path)
        parsed = structure_resume_text(raw_text, light_client)
        result = merge_parsed_experience(db, parsed)
        # 新增/更新的 bullet 顺带做一次关键词/行为/结果三元组抽取，供后面
        # 简历重制阶段做关键词匹配用。抽取失败不应该让整个上传流程失败——
        # 三元组只是衍生索引，没有它简历重制仍然能跑，只是匹配会更粗。
        triads_backfilled = 0
        try:
            triads_backfilled = backfill_bullet_triads(db, light_client)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001 - 面向用户的友好提示，细节已经包含在异常信息里
        return _redirect_with_flash("/dashboard/profile", f"简历解析失败: {exc}", error=True)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    msg = (
        f"解析完成：新增 {result.companies_added} 家公司、{result.positions_added} 段经历、"
        f"{result.bullets_added} 条贡献句（跳过 {result.bullets_skipped_duplicate} 条重复），"
        f"已为其中 {triads_backfilled} 条贡献句提炼关键词"
    )
    return _redirect_with_flash("/dashboard/profile", msg)


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

    try:
        resume_version = confirm_and_finalize(db, jd_id, k_value, hit_items, accepted_suggestions)
    except TailorJDNotFoundError:
        raise HTTPException(status_code=404, detail="JD not found")
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
            **_flash_params(request),
        },
    )
