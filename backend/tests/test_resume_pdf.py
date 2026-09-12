"""Phase 2 补完：简历重制的 PDF 产出（app/services/resume_pdf.py）。

覆盖：默认风格模板能正确渲染基本信息和经历列表、未知 style_id 报错、
生成的字节确实是合法 PDF、落盘路径正确、以及"不同长度简历排版是否稳定
不错位"这条自测要求——用 WeasyPrint 自己的分页结果做验证，短简历应该是
一页，明显更长的简历应该跨页而不是报错或者把内容截断成一页挤爆。
"""

from __future__ import annotations

import importlib
import sys

import pytest
from weasyprint import HTML

from app.services import resume_pdf
from app.services.resume_pdf import (
    PdfRenderingUnavailableError,
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


def test_render_resume_pdf_bytes_raises_friendly_error_when_weasyprint_unavailable(monkeypatch):
    """回归测试：本地环境缺系统级 Pango/GObject 库（Windows 上最常见，
    `pip install weasyprint` 装不上这些）时，之前的版本会在模块 import
    阶段直接崩掉、拖垮整个 App 启动；现在改成运行时才检查，这里验证检查
    到位——不能用就抛信息明确的 `PdfRenderingUnavailableError`，而不是让
    调用方直接看到一个原始的 cffi/OSError。"""
    monkeypatch.setattr(resume_pdf, "_WeasyPrintHTML", None)
    monkeypatch.setattr(resume_pdf, "_WEASYPRINT_IMPORT_ERROR", ImportError("simulated missing libgobject-2.0-0"))

    with pytest.raises(PdfRenderingUnavailableError) as exc_info:
        render_resume_pdf_bytes(_short_resume())

    assert "GTK3" in str(exc_info.value) or "Pango" in str(exc_info.value)


def test_module_import_survives_real_weasyprint_import_failure():
    """最贴近真实故障场景的回归测试：不是"调用某个函数时环境缺依赖"，而是
    "这个模块被 import 的那一刻，`import weasyprint` 本身就抛异常"——这正是
    Windows 上真实发生过的情况（缺 libgobject-2.0-0，`from weasyprint import
    HTML` 直接在模块顶层崩掉，进而拖垮 `resume_tailor.py` ->
    `routes_dashboard.py` -> `app.main` 整条 import 链）。用
    `sys.modules['weasyprint'] = None` 这个 Python 标准技巧强制让接下来的
    `import weasyprint` 抛 ImportError，重新加载 `resume_pdf` 模块，断言
    重新加载本身不抛异常、且模块记录下了"这次不可用"这个状态。"""
    original_weasyprint = sys.modules.get("weasyprint")
    sys.modules["weasyprint"] = None  # None 是文档化的写法：强制下一次 import 失败
    try:
        reloaded = importlib.reload(resume_pdf)
        assert reloaded._WeasyPrintHTML is None
        assert reloaded._WEASYPRINT_IMPORT_ERROR is not None
        # 用重新加载之后模块自己的异常类，而不是文件顶部 reload 之前导入的
        # 那个引用——`importlib.reload` 会重新执行类定义,产生一个新的类
        # 对象,和 reload 之前 import 进来的旧类对象不是同一个身份,
        # `pytest.raises(旧引用)` 抓不住新对象抛出的实例。
        with pytest.raises(reloaded.PdfRenderingUnavailableError):
            reloaded.render_resume_pdf_bytes(_short_resume())
    finally:
        # 恢复真实的 weasyprint，避免这次 reload 的副作用影响同一个测试
        # 进程里其他测试用例（PDF 分页测试等都依赖真实的 WeasyPrint 可用）。
        if original_weasyprint is not None:
            sys.modules["weasyprint"] = original_weasyprint
        else:
            sys.modules.pop("weasyprint", None)
        importlib.reload(resume_pdf)


def test_render_resume_html_unaffected_when_weasyprint_unavailable(monkeypatch):
    # render_resume_html 只依赖 Jinja2，不应该受 WeasyPrint 是否可用影响——
    # Markdown/结构化数据这条链路完全独立于 PDF 渲染。
    monkeypatch.setattr(resume_pdf, "_WeasyPrintHTML", None)
    html = render_resume_html(_short_resume())
    assert "李明" in html


def test_pdf_layout_paginates_instead_of_crashing_or_truncating():
    """自测要求：不同长度简历排版稳定不错位。短简历应该是一页；明显更长
    的简历应该合理跨到多页——如果渲染逻辑有问题（比如溢出内容被裁掉、
    或者死循环/报错），这条测试能第一时间发现。"""
    short_pages = HTML(string=render_resume_html(_short_resume())).render().pages
    assert len(short_pages) == 1

    long_pages = HTML(string=render_resume_html(_long_resume())).render().pages
    assert len(long_pages) > 1
