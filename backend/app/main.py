from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes_dashboard import router as dashboard_router
from app.api.routes_system import router as system_router
from app.core.config import get_settings
from app.core.migrate import run_migrations
from app.core.pairing import load_or_create_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jobpilot")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.ensure_home()
    run_migrations(settings)
    load_or_create_token(settings)  # 确保 pairing token 存在,首次启动即可生成
    logger.info("JobPilot local app ready. data dir = %s", settings.home)
    yield


app = FastAPI(title="JobPilot Local App", version="0.1.0", lifespan=lifespan)
app.include_router(system_router)
app.include_router(dashboard_router)


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
