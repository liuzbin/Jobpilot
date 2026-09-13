"""根据 model_config 表里保存的配置,构造对应槽位（轻量/重量）的 LLMClient。

Phase 5 起，这里返回的不再是裸的 `OpenAICompatibleClient`，而是外面包了一层
`UsageTrackingLLMClient`（见 `app/core/llm_usage.py`）——每次调用无论成功
失败都会记一笔 `LLMUsageLog`，供 Dashboard 的用量统计面板使用。这一层包装
只在这里（真正读取用户配置、构造真实网络客户端的地方）加，不影响测试：
`tests/conftest.py` 里的用例都是通过 `app.dependency_overrides` 直接把
`get_light_client`/`get_heavy_client` 换成 `FakeLLMClient`，根本不会调用
到 `build_client`，也就不会产生用量记录。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.llm_client import LLMClient, OpenAICompatibleClient
from app.core.llm_usage import UsageTrackingLLMClient
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
    inner = OpenAICompatibleClient(base_url=row.base_url, api_key=api_key, model=row.model_name)
    return UsageTrackingLLMClient(inner=inner, db=db, slot=slot, model_name=row.model_name)
