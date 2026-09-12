"""
Phase 2 补完：简历重制模块的 PDF 产出（实施方案 5.3："生成管线上……再用
WeasyPrint 渲染成 PDF。当前先实现一套风格模板,模板和渲染逻辑用 style_id
解耦"）。

`confirm_and_finalize` 产出的 `resume_json` 是唯一的事实来源——PDF 只是它的
一种渲染形式，不会在渲染过程中引入任何新内容，样式模板负责的只是排版。
`style_id` 对应 `app/templates/resume_styles/<style_id>.html` 下的一个 Jinja2
模板文件，目前只有 "default" 一套风格，后续要加新风格只需要新增模板文件，
不需要改这里的渲染逻辑。
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape
from weasyprint import HTML

from app.core.config import get_settings

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates" / "resume_styles"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


class UnknownResumeStyleError(ValueError):
    pass


def render_resume_html(resume_json: dict, style_id: str = "default") -> str:
    """按 style_id 选模板，渲染出可以直接喂给 WeasyPrint 的 HTML 字符串。"""
    try:
        template = _env.get_template(f"{style_id}.html")
    except TemplateNotFound as exc:
        raise UnknownResumeStyleError(f"未知的简历风格：{style_id}") from exc
    return template.render(resume=resume_json or {})


def render_resume_pdf_bytes(resume_json: dict, style_id: str = "default") -> bytes:
    html_text = render_resume_html(resume_json, style_id)
    return HTML(string=html_text).write_pdf()


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
