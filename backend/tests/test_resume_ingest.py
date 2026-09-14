"""打磨阶段用户反馈第 2 点：`extract_and_structure` 是"规则优先、LLM 兜底"
路径的唯一入口。这里只测路由逻辑本身（谁负责产出结果、什么时候才会调用
LLM），具体的规则解析细节已经在 test_resume_md_parser.py 里覆盖过了。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.llm_client import FakeLLMClient
from app.services.resume_ingest import extract_and_structure

TEMPLATE_CONFORMANT_MD = """\
# Jane Doe

## Professional Experience

**Acme Corp** | *Engineer*
<div align="right"><i>2020 – 2022</i></div>

- Did a thing.
"""

FREEFORM_MD = """\
Just some notes about my career, no headings at all.
"""


def test_md_file_matching_template_uses_rule_based_parser_and_skips_llm(tmp_path):
    file_path = tmp_path / "resume.md"
    file_path.write_text(TEMPLATE_CONFORMANT_MD, encoding="utf-8")
    fake = FakeLLMClient(responses=[])  # 一旦真的调用 LLM 就会因为没有预设回复而报错

    result = extract_and_structure(file_path, TEMPLATE_CONFORMANT_MD, fake)

    assert fake.calls == []
    assert result["companies"][0]["company_name"] == "Acme Corp"


def test_md_file_not_matching_template_falls_back_to_llm(tmp_path):
    file_path = tmp_path / "resume.md"
    file_path.write_text(FREEFORM_MD, encoding="utf-8")
    fake = FakeLLMClient(responses=[{"basic": {"full_name": "Jane Doe"}, "companies": []}])

    result = extract_and_structure(file_path, FREEFORM_MD, fake)

    assert len(fake.calls) == 1
    assert result["basic"]["full_name"] == "Jane Doe"
    assert result["education_entries"] == []
    assert result["projects"] == []


def test_non_md_file_always_uses_llm_even_if_content_looks_like_template(tmp_path):
    """即便一个 .txt 文件里的内容碰巧长得跟模板一样，规则解析器也不应该被
    调用——规则解析只信任 .md 后缀，其它格式一律走 LLM 兜底。"""
    file_path = tmp_path / "resume.txt"
    file_path.write_text(TEMPLATE_CONFORMANT_MD, encoding="utf-8")
    fake = FakeLLMClient(responses=[{"basic": {}, "companies": [{"company_name": "From LLM", "positions": []}]}])

    result = extract_and_structure(file_path, TEMPLATE_CONFORMANT_MD, fake)

    assert len(fake.calls) == 1
    assert result["companies"][0]["company_name"] == "From LLM"


def test_structure_resume_text_raises_on_blank_input():
    from app.services.resume_ingest import structure_resume_text

    fake = FakeLLMClient(responses=[])
    with pytest.raises(ValueError):
        structure_resume_text("   ", fake)
    assert fake.calls == []
