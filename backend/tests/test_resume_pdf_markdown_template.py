"""打磨阶段用户反馈第 1 点：MD 模板渲染出来的 Markdown 转 PDF（和内置
default/compact 两套 CSS 风格是完全独立的一条渲染路径，见 resume_pdf.py
"打磨阶段新增" 那一节）。"""

from __future__ import annotations

import pytest

from app.services import resume_pdf
from app.services.resume_pdf import (
    PdfRenderingUnavailableError,
    render_markdown_resume_html,
    render_markdown_resume_pdf_bytes,
    render_markdown_to_html_body,
    save_markdown_resume_pdf,
)


def test_render_markdown_to_html_body_converts_headings_and_lists():
    body = render_markdown_to_html_body("## Hello\n\n- one\n- two\n")
    assert "<h2>Hello</h2>" in body
    assert "<li>one</li>" in body
    assert "<li>two</li>" in body


def test_render_markdown_to_html_body_passes_through_raw_html():
    """模板里内嵌的 <div align="right">/<i> 这类原始 HTML 应该原样穿透，
    这是默认模板排版（日期右对齐）能生效的前提。"""
    body = render_markdown_to_html_body('<div align="right"><i>Feb 2025 – 至今</i></div>')
    assert '<div align="right">' in body
    assert "<i>Feb 2025" in body


def test_render_markdown_resume_html_wraps_in_generic_style():
    html_text = render_markdown_resume_html("# Title")
    assert "<style>" in html_text
    assert "<h1>Title</h1>" in html_text


def test_render_markdown_resume_pdf_bytes_produces_valid_pdf():
    pdf_bytes = render_markdown_resume_pdf_bytes("# Jane Doe\n\n## Experience\n\n- Did a thing\n")
    assert pdf_bytes[:4] == b"%PDF"


def test_render_markdown_resume_pdf_bytes_raises_when_weasyprint_unavailable(monkeypatch):
    monkeypatch.setattr(resume_pdf, "_WeasyPrintHTML", None)
    monkeypatch.setattr(resume_pdf, "_WEASYPRINT_IMPORT_ERROR", ImportError("simulated missing libgobject"))
    with pytest.raises(PdfRenderingUnavailableError):
        render_markdown_resume_pdf_bytes("# Title")


def test_save_markdown_resume_pdf_writes_file(tmp_path, monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "home", tmp_path)
    path = save_markdown_resume_pdf(123, "# Title\n\n- bullet\n")
    assert path.exists()
    assert path.name == "resume_123.pdf"
    assert path.read_bytes()[:4] == b"%PDF"
