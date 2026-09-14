"""
Phase 2 补完：简历重制模块的 PDF 产出（实施方案 5.3："生成管线上……再用
WeasyPrint 渲染成 PDF。当前先实现一套风格模板,模板和渲染逻辑用 style_id
解耦"）。

`confirm_and_finalize` 产出的 `resume_json` 是唯一的事实来源——PDF 只是它的
一种渲染形式，不会在渲染过程中引入任何新内容，样式模板负责的只是排版。
`style_id` 对应 `app/templates/resume_styles/<style_id>.html` 下的一个 Jinja2
模板文件；新增一套风格只需要新增一个模板文件 + 在 `AVAILABLE_RESUME_STYLES`
里登记一行给用户看的名字，不需要改这里的渲染逻辑——两套模板读的是完全相同
的 `resume_json` 结构，互相切换只影响排版，不会丢失或改变任何简历内容。

**Phase 5"简历风格自定义能力"**：`AVAILABLE_RESUME_STYLES` 是 Dashboard 上
风格下拉框的唯一数据来源（`routes_dashboard.py` 渲染下拉框、校验用户提交的
`style_id` 都读这个字典，不是各自维护一份可能会不同步的列表）。用户可以在
"生成简历"确认页选风格，也可以在已经生成的简历结果页不重新走 LLM 生成、
只用已经存好的 `resume_json` 换一套风格重新渲染 PDF（见
`resume_tailor.regenerate_resume_pdf`）——这一步完全本地渲染、不消耗任何
LLM 调用，所以"换个风格看看"可以随便试。

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

import markdown as _markdown_lib
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape

from app.core.config import get_settings
from app.core.paths import app_root

_TEMPLATES_DIR = app_root() / "app" / "templates" / "resume_styles"

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


# style_id -> 给用户看的中文名。顺序就是下拉框里出现的顺序。新增风格模板
# 之后必须同步在这里登记一行，否则用户在 Dashboard 上永远选不到它——这是
# 有意的（不自动扫描 resume_styles/ 目录下多出来的 .html 文件当成可选项），
# 避免"模板文件还没写完/写错了"就意外出现在用户可选列表里。
AVAILABLE_RESUME_STYLES: dict[str, str] = {
    "default": "默认（简洁单栏）",
    "compact": "紧凑（更小间距，适合内容较多）",
}


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
            "PDF 渲染依赖未就绪：WeasyPrint 需要系统级的 Pango/GObject 库。"
            "Windows 上本地 App 启动时会自动检测并尝试静默安装 GTK3 Runtime"
            "（详见启动日志里"
            "\"PDF 渲染依赖检查：...\"这一行）——如果日志说已经自动装好了，"
            "重启一次本地 App 即可；如果日志说自动安装失败了，可以按日志里给的"
            "链接手动安装。不影响简历的 Markdown/结构化数据内容。"
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


# ---------- 打磨阶段新增：MD 模板驱动的渲染（见 resume_template_service.py） ----------
#
# 这一路和上面 style_id 驱动的两套内置风格完全独立：不复用 AVAILABLE_RESUME_STYLES，
# 也不新增进那个字典——MD 模板的"排版"是模板内容本身决定的，这里只负责
# "把渲染好的 Markdown 转成 PDF"这一步通用逻辑，套的是固定的一份极简 CSS
# （_markdown_generic.html），不针对任何一个具体模板定制样式。

MD_TEMPLATE_STYLE_SENTINEL = "md_template"


def render_markdown_to_html_body(markdown_text: str) -> str:
    """Markdown 源文本转 HTML 片段。`extra`/`sane_lists` 这两个扩展是
    python-markdown 官方内置的，分别补上表格/代码块等常见写法、以及更符合
    直觉的列表解析规则；模板里直接内嵌的原始 HTML（比如 <div align="right">）
    默认就会被原样保留穿透，不需要额外配置。"""
    return _markdown_lib.markdown(markdown_text or "", extensions=["extra", "sane_lists"])


def render_markdown_resume_html(markdown_text: str) -> str:
    body_html = render_markdown_to_html_body(markdown_text)
    template = _env.get_template("_markdown_generic.html")
    return template.render(body_html=body_html)


def render_markdown_resume_pdf_bytes(markdown_text: str) -> bytes:
    if _WeasyPrintHTML is None:
        raise PdfRenderingUnavailableError(
            "PDF 渲染依赖未就绪：WeasyPrint 需要系统级的 Pango/GObject 库。"
            "Windows 上本地 App 启动时会自动检测并尝试静默安装 GTK3 Runtime"
            "（详见启动日志里"
            "\"PDF 渲染依赖检查：...\"这一行）——如果日志说已经自动装好了，"
            "重启一次本地 App 即可；如果日志说自动安装失败了，可以按日志里给的"
            "链接手动安装。不影响简历的 Markdown/结构化数据内容。"
            f" 原始错误：{_WEASYPRINT_IMPORT_ERROR}"
        )
    html_text = render_markdown_resume_html(markdown_text)
    return _WeasyPrintHTML(string=html_text).write_pdf()


def save_markdown_resume_pdf(resume_version_id: int, markdown_text: str) -> Path:
    """和 save_resume_pdf 是同一个落盘约定（同一个目录、同一套文件名规则），
    只是渲染路径换成了"MD 模板 -> HTML -> PDF"这一条，供
    resume_tailor.confirm_and_finalize/regenerate_resume_pdf 在
    resume_template_id 非空时调用。"""
    settings = get_settings()
    resumes_dir = settings.home / "resumes"
    resumes_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = resumes_dir / f"resume_{resume_version_id}.pdf"
    pdf_path.write_bytes(render_markdown_resume_pdf_bytes(markdown_text))
    return pdf_path
