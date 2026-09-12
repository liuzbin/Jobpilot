"""插件（Chrome Extension）专用的 REST 接口。

和 `routes_system.py` 里的 `/api/health` `/api/status` 一样走配对鉴权
（`require_paired_request`：校验 `X-JobPilot-Token` + Origin 必须是
`chrome-extension://` 开头），区别是这里放的是插件真正要用来"做事"的业务接口，
而不是连接状态探测。Phase 3 目前只有一个：插件在职位详情页（当前只做了
LinkedIn）解析出 JD 信息后，一键发过来入库，直接复用 Phase 1 已经跑通的
`jd_ingest.create_jd`，和 Dashboard 手动粘贴 JD 走的是完全同一条入库逻辑，
入库之后仍然停在 `pending` 状态，用户在 Dashboard 里点"分析"才会真正调用 LLM
打分——插件这一步只负责"送进来"，不越权替用户触发消耗 API 配额的分析动作。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_paired_request
from app.core.config import get_settings
from app.core.db import get_db
from app.services.jd_ingest import create_jd

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
