"""Phase 2 补完：简历重制的 PDF 产出（app/services/resume_pdf.py）。

覆盖：默认风格模板能正确渲染基本信息和经历列表、未知 style_id 报错、
生成的字节确实是合法 PDF、落盘路径正确、以及"不同长度简历排版是否稳定
不错位"这条自测要求——用 WeasyPrint 自己的分页结果做验证，短简历应该是
一页，明显更长的简历应该跨页而不是报错或者把内容截断成一页挤爆。
"""

from __future__ import annotations

import pytest
from weasyprint import HTML

from app.services.resume_pdf import (
    UnknownResumeStyleError,
    render_resume_html,
    render_resume_pdf_bytes,
    save_resume_pdf,
)


def _short_resume() -> dict:
    return {
        "basic": {
            "full_name": "李明",
            "target_title": "数据工程师",
            "email": "li@example.com",
            "phone": "138-0000-0000",
        },
        "experience": [
            {
                "position_title": "Data Engineer",
                "project_name": "Big Data Platform",
                "items": [
                    {"action_summary": "搭建了 Hadoop ETL 流水线", "result_summary": "每日处理 2TB 日志"},
                ],
            }
        ],
    }


def _long_resume(num_positions: int = 8, bullets_per_position: int = 10) -> dict:
    return {
        "basic": {"full_name": "李明", "target_title": "数据工程师"},
        "experience": [
            {
                "position_title": f"Position {i}",
                "project_name": f"Project {i}",
                "items": [
                    {
                        "action_summary": f"负责第 {j} 项具体工作，包含较长的描述文本用于撑满排版",
                        "result_summary": f"相关指标提升 {j}%",
                    }
                    for j in range(bullets_per_position)
                ],
            }
            for i in range(num_positions)
        ],
    }


def test_render_resume_html_includes_basic_and_experience_content():
    html = render_resume_html(_short_resume())
    assert "李明" in html
    assert "数据工程师" in html
    assert "Hadoop ETL" in html
    assert "li@example.com" in html


def test_render_resume_html_empty_experience_shows_placeholder_not_crash():
    html = render_resume_html({"basic": {}, "experience": []})
    assert "没有命中" in html


def test_render_resume_html_unknown_style_raises():
    with pytest.raises(UnknownResumeStyleError):
        render_resume_html(_short_resume(), style_id="does-not-exist")


def test_render_resume_pdf_bytes_produces_valid_pdf():
    pdf_bytes = render_resume_pdf_bytes(_short_resume())
    assert pdf_bytes[:4] == b"%PDF"
    assert pdf_bytes.rstrip().endswith(b"%%EOF")


def test_save_resume_pdf_writes_file_to_disk(isolated_home):
    from app.core.config import get_settings

    path = save_resume_pdf(42, _short_resume())
    assert path.exists()
    assert path == get_settings().home / "resumes" / "resume_42.pdf"
    assert path.read_bytes()[:4] == b"%PDF"


def test_pdf_layout_paginates_instead_of_crashing_or_truncating():
    """自测要求：不同长度简历排版稳定不错位。短简历应该是一页；明显更长
    的简历应该合理跨到多页——如果渲染逻辑有问题（比如溢出内容被裁掉、
    或者死循环/报错），这条测试能第一时间发现。"""
    short_pages = HTML(string=render_resume_html(_short_resume())).render().pages
    assert len(short_pages) == 1

    long_pages = HTML(string=render_resume_html(_long_resume())).render().pages
    assert len(long_pages) > 1
