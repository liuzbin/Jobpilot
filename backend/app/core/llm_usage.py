"""
Phase 5"用量统计面板"的采集入口：包一层 LLMClient，在每次调用之后（无论
成功还是失败）落一条 `LLMUsageLog` 记录。

为什么不直接让 `OpenAICompatibleClient` 自己写库：它不知道自己这次是被
当成"轻量"还是"重量"槽位在用、也不该自己持有一个 DB Session——构造它的
地方（`app.core.llm_factory.build_client`）才同时知道这两件事、也已经拿着
当次请求的 Session。用一层薄薄的包装类把"调用模型"和"记一笔账"分开，这样
`OpenAICompatibleClient` 保持只关心"怎么发一次请求"，也方便测试——这里的
包装逻辑只依赖 `LLMClient` 协议，不关心内层到底是真的
`OpenAICompatibleClient` 还是测试用的 `FakeLLMClient`。

只统计调用次数和 token 数，不做费用估算——理由见 `app/models/tables.py`
里 `LLMUsageLog` 的类文档字符串。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.llm_client import LLMCallError, LLMClient
from app.models.tables import LLMUsageLog, ModelSlot


def record_llm_usage(
    db: Session,
    *,
    slot: ModelSlot,
    model_name: str | None,
    ok: bool,
    usage: dict | None,
    error_message: str | None,
) -> LLMUsageLog:
    usage = usage or {}
    entry = LLMUsageLog(
        slot=slot,
        model_name=model_name,
        ok=ok,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        error_message=error_message,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@dataclass
class UsageTrackingLLMClient:
    """包一层 LLMClient：调用成功/失败都记一笔 `LLMUsageLog`，本身完全不
    改变 `complete_json` 的行为——成功照常返回、失败照常抛 `LLMCallError`，
    对上层业务代码（画像抽取、打分、简历重制……）完全透明，它们不需要知道
    自己拿到的客户端其实被包了一层。"""

    inner: LLMClient
    db: Session
    slot: ModelSlot
    model_name: str | None

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        try:
            result = self.inner.complete_json(system_prompt, user_prompt)
        except LLMCallError as exc:
            record_llm_usage(
                self.db,
                slot=self.slot,
                model_name=self.model_name,
                ok=False,
                usage=None,
                error_message=str(exc),
            )
            raise
        # `getattr` 而不是直接取属性：`inner` 在生产环境里是
        # `OpenAICompatibleClient`（有 `last_usage`），但这层包装本身只依赖
        # `LLMClient` 协议，不应该假设内层一定是这个具体类型。
        usage = getattr(self.inner, "last_usage", None)
        record_llm_usage(
            self.db,
            slot=self.slot,
            model_name=self.model_name,
            ok=True,
            usage=usage,
            error_message=None,
        )
        return result
