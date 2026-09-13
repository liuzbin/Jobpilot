"""Phase 2 补完：简历重制的 PDF 产出（app/services/resume_pdf.py）。

覆盖：默认风格模板能正确渲染基本信息和经历列表、未知 style_id 报错、
生成的字节确实是合法 PDF、落盘路径正确、以及"不同长度简历排版是否稳定
不错位"这条自测要求——用 WeasyPrint 自己的分页结果做验证，短简历应该是
一页，明显更长的简历应该跨页而不是报错或者把内容截断成一页挤爆。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from weasyprint import HTML

from app.services import resume_pdf
from app.services.resume_pdf import (
    AVAILABLE_RESUME_STYLES,
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
    `routes_dashboard.py` -> `app.main` 整条 import 链）。

    Phase 5 时改成了在一个全新的子进程里验证，而不是像最初那样在当前测试
    进程里用 `importlib.reload(resume_pdf)` 模拟——见 DEVELOPMENT_LOG.md
    "问题一"：`importlib.reload` 会重新执行 `resume_pdf.py` 里的类定义,
    产生和 reload 之前不是同一个身份的新 `UnknownResumeStyleError`/
    `PdfRenderingUnavailableError` 类对象；`resume_tailor.py` 在自己
    模块顶层 `from app.services.resume_pdf import UnknownResumeStyleError`
    这种写法绑定的是 reload 之前的旧类对象,不会跟着 reload 更新,导致
    reload 发生之后,任何依赖 `except UnknownResumeStyleError` /
    `pytest.raises(UnknownResumeStyleError)` 做身份匹配的代码都可能因为
    两边其实是两个不同的类对象而匹配失败——这个副作用会一直残留到当前
    pytest 进程结束为止,不只影响这一个测试文件。子进程方案完全避免了这个
    问题：子进程结束就销毁,不会污染当前测试进程里其他模块已经绑定好的类
    引用,而且也更贴近真实场景——用户机器上真的是"整个进程刚启动、
    `import weasyprint` 第一次执行就失败"，不是"进程运行中途重新加载一个
    模块"。"""
    script = (
        "import sys; sys.modules['weasyprint'] = None\n"
        "from app.services import resume_pdf\n"
        "assert resume_pdf._WeasyPrintHTML is None\n"
        "assert resume_pdf._WEASYPRINT_IMPORT_ERROR is not None\n"
        "resume_pdf.render_resume_pdf_bytes({'basic': {}, 'experience': []})\n"
    )
    backend_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(backend_root),
        capture_output=True,
        text=True,
        timeout=30,
    )
    # 最后一行 render_resume_pdf_bytes(...) 应该抛出 PdfRenderingUnavailableError
    # 让子进程以非 0 退出；如果模块导入本身就崩了（回归到旧行为），子进程
    # 也会非 0 退出但 stderr 里不会是这个信息明确的异常,而是原始的
    # ImportError/OSError,下面这条断言能把两种失败方式区分开。
    assert result.returncode != 0, result.stderr
    assert "PdfRenderingUnavailableError" in result.stderr, result.stderr


def test_render_resume_html_unaffected_when_weasyprint_unavailable(monkeypatch):
    # render_resume_html 只依赖 Jinja2，不应该受 WeasyPrint 是否可用影响——
    # Markdown/结构化数据这条链路完全独立于 PDF 渲染。
    monkeypatch.setattr(resume_pdf, "_WeasyPrintHTML", None)
    html = render_resume_html(_short_resume())
    assert "李明" in html


def test_available_resume_styles_all_have_a_real_template_file(isolated_home):
    """Phase 5"简历风格自定义能力"：`AVAILABLE_RESUME_STYLES` 是 Dashboard
    风格下拉框的唯一数据来源，这里锁定一条前提——里面登记的每一个 style_id
    都必须真的对应一个能渲染的模板文件，不能出现"下拉框里有、选了就 404"
    这种情况。"""
    assert "default" in AVAILABLE_RESUME_STYLES
    assert "compact" in AVAILABLE_RESUME_STYLES
    for style_id in AVAILABLE_RESUME_STYLES:
        html = render_resume_html(_short_resume(), style_id=style_id)
        assert "李明" in html


def test_compact_style_renders_same_content_as_default():
    """两套风格模板必须读同一份 resume_json、只是排版不同——用同一份数据
    分别渲染两套风格，断言两边都完整包含关键信息，不会因为换了个模板就
    丢内容（比如漏渲染某个字段）。"""
    resume = _short_resume()
    default_html = render_resume_html(resume, style_id="default")
    compact_html = render_resume_html(resume, style_id="compact")
    for html in (default_html, compact_html):
        assert "李明" in html
        assert "数据工程师" in html
        assert "Hadoop ETL" in html
        assert "li@example.com" in html


def test_compact_style_produces_valid_pdf():
    pdf_bytes = render_resume_pdf_bytes(_short_resume(), style_id="compact")
    assert pdf_bytes[:4] == b"%PDF"
    assert pdf_bytes.rstrip().endswith(b"%%EOF")


def test_pdf_layout_paginates_instead_of_crashing_or_truncating():
    """自测要求：不同长度简历排版稳定不错位。短简历应该是一页；明显更长
    的简历应该合理跨到多页——如果渲染逻辑有问题（比如溢出内容被裁掉、
    或者死循环/报错），这条测试能第一时间发现。"""
    short_pages = HTML(string=render_resume_html(_short_resume())).render().pages
    assert len(short_pages) == 1

    long_pages = HTML(string=render_resume_html(_long_resume())).render().pages
    assert len(long_pages) > 1
