"""根据 model_config 表里保存的配置,构造对应槽位（轻量/重量）的 LLMClient。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.llm_client import LLMClient, OpenAICompatibleClient
from app.core.secrets import get_secret
from app.models.tables import ModelConfig, ModelSlot


class ModelNotConfiguredError(RuntimeError):
    pass


def build_client(db: Session, settings: Settings, slot: ModelSlot) -> LLMClient:
    row = db.query(ModelConfig).filter(ModelConfig.slot == slot).one_or_none()
    if row is None or not row.base_url or not row.model_name:
        raise ModelNotConfiguredError(
            f"{slot.value} 模型还没有配置,请先到 Dashboard 的模型配置页面填写 base_url / model_name / api_key"
        )
    api_key = get_secret(settings, row.keyring_ref) or ""
    return OpenAICompatibleClient(base_url=row.base_url, api_key=api_key, model=row.model_name)
