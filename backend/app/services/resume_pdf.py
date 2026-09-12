"""
Phase 2 补完：简历重制模块的 PDF 产出（实施方案 5.3："生成管线上……再用
WeasyPrint 渲染成 PDF。当前先实现一套风格模板,模板和渲染逻辑用 style_id
解耦"）。

`confirm_and_finalize` 产出的 `resume_json` 是唯一的事实来源——PDF 只是它的
一种渲染形式，不会在渲染过程中引入任何新内容，样式模板负责的只是排版。
`style_id` 对应 `app/templates/resume_styles/<style_id>.html` 下的一个 Jinja2
模板文件，目前只有 "default" 一套风格，后续要加新风格只需要新增模板文件，
不需要改这里的渲染逻辑。

**WeasyPrint 导入做成延迟/防御性的（Phase 4 之后补的健壮性修复）**：
WeasyPrint 不是纯 Python 库，它通过 cffi 调用系统里的 Pango/GObject 这些
C 语言库，`pip install weasyprint` 只装了 Python 这一层，底层的系统库需要
用户自己额外安装（Windows 上尤其容易漏掉，需要单独装一份 GTK3 运行时）。
这个模块曾经在文件顶层直接 `from weasyprint import HTML`——一旦用户机器上
没装好这些系统库，这一行 import 就会在模块被加载的那一刻直接抛异常,而这个
模块又被 `resume_tailor.py` 在顶层 import,`resume_tailor.py` 又被
`routes_dashboard.py` 在顶层 import,最终导致整个 `app.main` 从启动的第一步
就崩溃退出——但实际上"生成 PDF"只是简历重制流程里的一个可选产物（真正的
事实来源是 `resume_json`/`markdown_text`），画像、JD 打分、抓取、题库这些
完全不相关的功能不应该被这一个缺失的系统依赖拖累到连本地 App 都启动不起来。
所以这里改成运行时按需 import：模块加载阶段只记录"能不能用"，真正调用
`render_resume_pdf_bytes` 的时候才检查,不能用就抛一个信息明确的
`PdfRenderingUnavailableError`,而不是让调用方看到一个不知所云的
`OSError: cannot load library 'libgobject-2.0-0'`。调用方
`resume_tailor.confirm_and_finalize` 早就把 `save_resume_pdf` 包在
try/except 里、失败了只是不写 `pdf_path`、不影响简历确认这个操作本身
（这一点在这次修复之前就是对的,是唯一挡住"import 崩了导致 App 也无法
使用"这个问题所必须补的那一半）。
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape

from app.core.config import get_settings

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates" / "resume_styles"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)

try:
    from weasyprint import HTML as _WeasyPrintHTML

    _WEASYPRINT_IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # noqa: BLE001 - 任何原因导致的导入失败都不应该拖垮整个 App
    _WeasyPrintHTML = None
    _WEASYPRINT_IMPORT_ERROR = _exc


class UnknownResumeStyleError(ValueError):
    pass


class PdfRenderingUnavailableError(RuntimeError):
    """WeasyPrint 本身没装好（通常是缺系统级的 Pango/GObject 库），
    不是代码逻辑问题，调用方应该把这个和"渲染出错"区分开，给用户一个
    "去装依赖"而不是"去查代码 bug"的提示。"""


def render_resume_html(resume_json: dict, style_id: str = "default") -> str:
    """按 style_id 选模板，渲染出可以直接喂给 WeasyPrint 的 HTML 字符串。"""
    try:
        template = _env.get_template(f"{style_id}.html")
    except TemplateNotFound as exc:
        raise UnknownResumeStyleError(f"未知的简历风格：{style_id}") from exc
    return template.render(resume=resume_json or {})


def render_resume_pdf_bytes(resume_json: dict, style_id: str = "default") -> bytes:
    if _WeasyPrintHTML is None:
        raise PdfRenderingUnavailableError(
            "PDF 渲染依赖未就绪：WeasyPrint 需要系统级的 Pango/GObject 库，"
            "Windows 上通常需要单独安装 GTK3 运行时"
            "（参考 https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#windows），"
            "装好之后重启本地 App 即可，不影响简历的 Markdown/结构化数据内容。"
            f" 原始错误：{_WEASYPRINT_IMPORT_ERROR}"
        )
    html_text = render_resume_html(resume_json, style_id)
    return _WeasyPrintHTML(string=html_text).write_pdf()


def save_resume_pdf(resume_version_id: int, resume_json: dict, style_id: str = "default") -> Path:
    """渲染并落盘到 `JOBPILOT_HOME/resumes/resume_<id>.pdf`，返回绝对路径。
    调用方（resume_tailor.confirm_and_finalize）负责把这个路径写回
    ResumeVersion.pdf_path。"""
    settings = get_settings()
    resumes_dir = settings.home / "resumes"
    resumes_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = resumes_dir / f"resume_{resume_version_id}.pdf"
    pdf_path.write_bytes(render_resume_pdf_bytes(resume_json, style_id))
    return pdf_path
