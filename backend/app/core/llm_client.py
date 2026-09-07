"""
LLM 调用的统一抽象。

设计目的：
1. 上层业务代码（画像抽取、JD 解析、打分、简历重制……）不应该关心具体接的是
   哪家模型服务，只需要拿到一个实现了 LLMClient 协议的对象,调用
   complete_json(...) 拿到结构化结果。
2. 只要目标服务提供 OpenAI 兼容的 Chat Completions 接口（base_url + api_key +
   model_name），就可以直接接入，不区分 OpenAI / DeepSeek / 其他兼容网关。
3. 测试时不应该真的发网络请求（没有真实 API Key,也不应该让测试依赖外部服务
   的可用性），所以提供 FakeLLMClient,测试里注入固定的返回值。

MVP 阶段先不引入真正的 "response_format: json_schema" 强校验（不同厂商对
Structured Outputs 的支持程度不一样),而是要求模型输出一段 JSON 文本,
在这一层做解析和基本的容错（去掉可能存在的 markdown 代码块包裹）。后续如果
接的模型支持严格的 JSON Schema 校验,可以在 OpenAICompatibleClient 内部按需
开启,不影响上层调用方的接口。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

import httpx


class LLMCallError(RuntimeError):
    """LLM 调用失败（网络错误、鉴权失败、返回内容无法解析成 JSON 等）统一抛这个,
    上层可以捕获后转成对用户友好的提示。"""


class LLMClient(Protocol):
    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        """发送一次对话请求,期望模型返回一段可以解析成 JSON 对象的文本,
        解析后以 dict 形式返回。解析失败或调用失败都应该抛 LLMCallError。"""
        ...


def _extract_json(text: str) -> dict:
    """模型有时会在 JSON 前后加 ```json ... ``` 包裹或者一些说明文字,这里做一次
    宽松提取：优先找最外层的一对花括号。"""
    text = text.strip()
    if text.startswith("```"):
        # 去掉 ```json 或 ``` 开头,以及结尾的 ```
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 兜底：截取第一个 { 到最后一个 } 之间的内容再尝试一次
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMCallError(f"模型返回内容无法解析成 JSON: {exc}") from exc
    raise LLMCallError("模型返回内容中没有找到 JSON 对象")


@dataclass
class OpenAICompatibleClient:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.0
    timeout_seconds: float = 60.0

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=self.timeout_seconds)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMCallError(f"调用模型接口失败: {exc}") from exc

        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMCallError(f"模型返回格式不符合预期: {data}") from exc
        return _extract_json(content)


class FakeLLMClient:
    """测试专用：按调用顺序返回预先准备好的固定结果,不发任何网络请求。"""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        self.calls.append((system_prompt, user_prompt))
        if not self._responses:
            raise LLMCallError("FakeLLMClient 没有更多预设返回值了")
        return self._responses.pop(0)
