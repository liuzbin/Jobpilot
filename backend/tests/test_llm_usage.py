"""
Phase 5"用量统计面板"：覆盖 `app/core/llm_usage.py`（`UsageTrackingLLMClient`
包装类 + `record_llm_usage`）和 `app/core/llm_factory.build_client` 的接线。

不测试真实网络请求——`OpenAICompatibleClient` 本身的网络行为已经不在这个
文件的覆盖范围内，这里只关心"包一层之后，调用成功/失败分别有没有正确记账"
这件事,用一个符合 `LLMClient` 协议的假客户端就够了。
"""

from __future__ import annotations

from app.core.llm_client import LLMCallError
from app.core.llm_usage import UsageTrackingLLMClient, record_llm_usage
from app.models.tables import LLMUsageLog, ModelSlot


class _FakeInner:
    """模拟 OpenAICompatibleClient：成功时把 usage 存在 last_usage 上，
    失败时抛 LLMCallError，和真实客户端的契约完全一致。"""

    def __init__(self, result: dict | None = None, usage: dict | None = None, error: str | None = None):
        self._result = result
        self.last_usage = usage
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        self.calls.append((system_prompt, user_prompt))
        if self._error:
            raise LLMCallError(self._error)
        return self._result


def test_record_llm_usage_stores_token_counts(db_session):
    entry = record_llm_usage(
        db_session,
        slot=ModelSlot.LIGHT,
        model_name="gpt-test",
        ok=True,
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        error_message=None,
    )
    assert entry.id is not None
    fetched = db_session.get(LLMUsageLog, entry.id)
    assert fetched.slot == ModelSlot.LIGHT
    assert fetched.ok is True
    assert fetched.prompt_tokens == 10
    assert fetched.completion_tokens == 5
    assert fetched.total_tokens == 15
    assert fetched.error_message is None


def test_record_llm_usage_handles_missing_usage_field(db_session):
    """不是所有模型网关都会返回 usage 字段——记下"调用过、但 token 数未知"，
    不应该报错或者硬凑一个 0。"""
    entry = record_llm_usage(
        db_session, slot=ModelSlot.HEAVY, model_name="m", ok=True, usage=None, error_message=None
    )
    assert entry.prompt_tokens is None
    assert entry.completion_tokens is None
    assert entry.total_tokens is None


def test_usage_tracking_client_records_success_and_returns_inner_result(db_session):
    inner = _FakeInner(result={"a": 1}, usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
    client = UsageTrackingLLMClient(inner=inner, db=db_session, slot=ModelSlot.LIGHT, model_name="light-model")

    result = client.complete_json("sys", "user")

    assert result == {"a": 1}
    assert inner.calls == [("sys", "user")]
    logs = db_session.query(LLMUsageLog).all()
    assert len(logs) == 1
    assert logs[0].ok is True
    assert logs[0].total_tokens == 5
    assert logs[0].model_name == "light-model"


def test_usage_tracking_client_records_failure_and_reraises(db_session):
    import pytest

    inner = _FakeInner(error="模拟调用失败")
    client = UsageTrackingLLMClient(inner=inner, db=db_session, slot=ModelSlot.HEAVY, model_name="heavy-model")

    with pytest.raises(LLMCallError):
        client.complete_json("sys", "user")

    logs = db_session.query(LLMUsageLog).all()
    assert len(logs) == 1
    assert logs[0].ok is False
    assert logs[0].error_message == "模拟调用失败"
    assert logs[0].total_tokens is None


def test_usage_tracking_client_tolerates_inner_without_last_usage(db_session):
    """内层客户端不一定是 OpenAICompatibleClient（协议上只要求实现
    complete_json），没有 last_usage 属性时不应该报 AttributeError。"""

    class _BareClient:
        def complete_json(self, system_prompt, user_prompt):
            return {"ok": True}

    client = UsageTrackingLLMClient(inner=_BareClient(), db=db_session, slot=ModelSlot.LIGHT, model_name="bare")
    result = client.complete_json("sys", "user")

    assert result == {"ok": True}
    logs = db_session.query(LLMUsageLog).all()
    assert len(logs) == 1
    assert logs[0].total_tokens is None


def test_build_client_wraps_with_usage_tracking(db_session):
    from app.core.config import get_settings
    from app.core.llm_factory import build_client
    from app.core.secrets import set_secret
    from app.models.tables import ModelConfig

    settings = get_settings()
    row = ModelConfig(slot=ModelSlot.LIGHT, base_url="https://api.example.com/v1", model_name="test-model")
    set_secret(settings, "test:key", "sk-test")
    row.keyring_ref = "test:key"
    db_session.add(row)
    db_session.commit()

    client = build_client(db_session, settings, ModelSlot.LIGHT)

    assert isinstance(client, UsageTrackingLLMClient)
    assert client.slot == ModelSlot.LIGHT
    assert client.model_name == "test-model"
