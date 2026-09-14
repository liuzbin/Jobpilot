"""
MD 简历模板库（打磨阶段用户反馈第 1 点的落地）。

用户反馈的原话是"简历只有两个格式，md 和 md 导出的同样式 PDF，风格只需要
控制 md 的格式就行了，不需要 css 渲染"——这里新增的"MD 模板"渲染方式就是
照这个思路做的：模板本身是一段带 Jinja2 占位符的 Markdown 源文本，生成简历
时把结构化数据喂进去渲染出最终 Markdown，PDF 只是这份 Markdown 转换出来的
一种展示形式（`resume_pdf.render_markdown_resume_pdf_bytes`），不像
`AVAILABLE_RESUME_STYLES` 那两套内置风格那样各自维护一份独立的 CSS 排版。

两套机制刻意并存、互不影响——这是打磨阶段确认过的范围："两套都留着，
用户可选"。用户即使一个 MD 模板都不上传，也完全不受影响，继续用内置的
default/compact 两套风格。

渲染时喂给模板的上下文结构（调用方是 `resume_tailor.py`）：
{
  "basic": {"full_name", "target_title", "email", "phone", "current_location",
            "github_url", "linkedin_url"},
  "summary": ["一行个人总结", ...],
  "skills": ["一行技能描述", ...],
  "experience": [
    {"company_name", "position_title", "project_name", "date_range",
     "bullet_items": [{"action_summary", "result_summary"}, ...]}
  ],
  "projects": [{"project_name", "date_range", "bullets": ["...", ...]}],
  "education": [{"school", "degree", "location", "date_range"}],
}
所有字段在模板里都允许缺失（Jinja2 默认 Undefined 打印成空字符串,不会
因为某个字段没填就直接渲染报错），模板作者要不要显示某个板块、显示成什么
格式，完全由模板内容自己决定——这也是"用户可以自定义 MD 模板"这个诉求
的核心：这里不強加任何 CSS/排版规则，只提供占位符和这份数据。
"""

from __future__ import annotations

import jinja2

from sqlalchemy.orm import Session

from app.models.tables import ResumeTemplate

# 默认模板：由用户上传的第一份简历（作为"默认简历格式"的范本）反推出来的
# Jinja2 模板，去掉具体人名/公司这些实际内容,换成占位符,排版约定原样保留
# （标题层级、<div align> 这些内嵌 HTML、每个板块的书写方式）。用户可以在
# 模板库里编辑它、把它设为非默认,或者完全无视它自己上传新模板——这里
# 只是保证"从空库开始也有一个能用的默认模板",不是唯一答案。
DEFAULT_TEMPLATE_NAME = "默认模板"
DEFAULT_TEMPLATE_MARKDOWN = """\
<div align="center">
  <h1>{{ basic.full_name }}</h1>
  <p>{{ basic.current_location }} | {{ basic.phone }} | <a href="mailto:{{ basic.email }}">{{ basic.email }}</a> | <a href="{{ basic.github_url }}">{{ basic.github_url }}</a></p>
</div>

## PROFESSIONAL SUMMARY

{% for line in summary %}
- {{ line }}
{% endfor %}

## TECHNICAL SKILLS

{% for line in skills %}
- {{ line }}
{% endfor %}

## PROFESSIONAL EXPERIENCE

{% for exp in experience %}
**{{ exp.company_name }}** | *{{ exp.position_title }}*
<div align="right"><i>{{ exp.date_range }}</i></div>

{% for item in exp.bullet_items %}
- {{ item.action_summary }}{% if item.result_summary %}，{{ item.result_summary }}{% endif %}
{% endfor %}

{% endfor %}

## PROJECT EXPERIENCE

{% for proj in projects %}
**{{ proj.project_name }}**
<div align="right"><i>{{ proj.date_range }}</i></div>

{% for bullet in proj.bullets %}
- {{ bullet }}
{% endfor %}

{% endfor %}

## EDUCATION

{% for edu in education %}
**{{ edu.school }}** | {{ edu.location }}
*{{ edu.degree }}*
<div align="right"><i>{{ edu.date_range }}</i></div>

{% endfor %}
"""

# 校验模板能不能正常渲染时用的一份合成上下文——不是真实用户数据,只是覆盖
# 每一个字段（含至少一条列表元素）,确保模板里但凡引用了这份契约里存在的
# 字段/循环变量,都不会在渲染时因为访问了不存在的属性而报错。真正暴露给
# 用户的渲染永远用 resume_tailor 传进来的真实数据,这份合成上下文只在
# "新增/编辑模板"这一步做一次性语法+字段引用校验。
_PREVIEW_CONTEXT = {
    "basic": {
        "full_name": "示例 姓名",
        "target_title": "示例职位",
        "email": "example@example.com",
        "phone": "+1 000-000-0000",
        "current_location": "示例城市",
        "github_url": "https://github.com/example",
        "linkedin_url": "https://linkedin.com/in/example",
    },
    "summary": ["示例个人总结第一条"],
    "skills": ["示例技能: A, B, C"],
    "experience": [
        {
            "company_name": "示例公司",
            "position_title": "示例岗位",
            "project_name": "示例项目",
            "date_range": "2020-01 – 至今",
            "bullet_items": [{"action_summary": "示例贡献行为", "result_summary": "示例结果"}],
        }
    ],
    "projects": [
        {
            "project_name": "示例独立项目",
            "date_range": "2021 – 2022",
            "bullets": ["示例项目贡献句"],
        }
    ],
    "education": [
        {
            "school": "示例学校",
            "degree": "示例学位",
            "location": "示例城市",
            "date_range": "2016-09 – 2020-06",
        }
    ],
}

_jinja_env = jinja2.Environment(
    autoescape=False,
    undefined=jinja2.Undefined,
    # trim_blocks/lstrip_blocks：{% for %}/{% endfor %} 这些块标签自己占的
    # 那一行不会在渲染结果里留下多余空行——Markdown 对空行数量比较敏感
    # （连续空行、标题前后空行都会影响渲染效果），模板作者写循环结构时
    # 不需要为了"渲染出来别有一堆空行"而把模板写得很别扭。
    trim_blocks=True,
    lstrip_blocks=True,
)


class ResumeTemplateNotFoundError(RuntimeError):
    pass


class ResumeTemplateRenderError(ValueError):
    """模板内容本身有问题（Jinja2 语法错误，或者渲染时触发了 Undefined 不
    允许的操作，比如对一个缺失字段做迭代），统一包成这个异常，给用户一个
    "模板哪里写错了"的清晰提示，而不是把 Jinja2 内部的异常类型和调用栈
    直接展示出来。"""


def render_template_markdown(template_content: str, context: dict) -> str:
    try:
        template = _jinja_env.from_string(template_content)
        return template.render(**context)
    except jinja2.TemplateSyntaxError as exc:
        raise ResumeTemplateRenderError(f"模板语法错误（第 {exc.lineno} 行）：{exc.message}") from exc
    except Exception as exc:  # noqa: BLE001 - Undefined 相关操作错误等,统一转成友好提示
        raise ResumeTemplateRenderError(f"模板渲染失败：{exc}") from exc


def validate_template_renders(content: str) -> None:
    """新增/编辑模板时调用：用合成上下文试渲染一次，模板写错了（哪怕只是
    笔误的占位符名）当场就能发现，而不是等用户真正生成简历那一刻才炸。"""
    render_template_markdown(content, _PREVIEW_CONTEXT)


def _ensure_single_default(db: Session, keep_id: int) -> None:
    db.query(ResumeTemplate).filter(ResumeTemplate.id != keep_id, ResumeTemplate.is_default.is_(True)).update(
        {"is_default": False}
    )


def list_templates(db: Session) -> list[ResumeTemplate]:
    return db.query(ResumeTemplate).order_by(ResumeTemplate.is_default.desc(), ResumeTemplate.id).all()


def get_template(db: Session, template_id: int) -> ResumeTemplate:
    template = db.get(ResumeTemplate, template_id)
    if template is None:
        raise ResumeTemplateNotFoundError(f"模板不存在：id={template_id}")
    return template


def get_or_create_default_template(db: Session) -> ResumeTemplate:
    """确保模板库至少有一个模板、且恰好一个是默认——库是空的时候用内置的
    `DEFAULT_TEMPLATE_MARKDOWN` 兜底建一条,和 `profile_service.get_or_create_profile_basic`
    是同样的"确保存在"模式。"""
    default = db.query(ResumeTemplate).filter(ResumeTemplate.is_default.is_(True)).one_or_none()
    if default is not None:
        return default
    existing = db.query(ResumeTemplate).order_by(ResumeTemplate.id).first()
    if existing is not None:
        existing.is_default = True
        db.commit()
        db.refresh(existing)
        return existing
    template = ResumeTemplate(name=DEFAULT_TEMPLATE_NAME, content=DEFAULT_TEMPLATE_MARKDOWN, is_default=True)
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def create_template(db: Session, name: str, content: str, set_default: bool = False) -> ResumeTemplate:
    name = (name or "").strip()
    content = content or ""
    if not name:
        raise ValueError("模板名称不能为空")
    if not content.strip():
        raise ValueError("模板内容不能为空")
    validate_template_renders(content)

    # 库里一个模板都没有时，新增的这一个自动就是默认——不能让画像"一个可用
    # 模板都没有"这种状态出现（生成简历页面选无可选）。
    is_first = db.query(ResumeTemplate).count() == 0
    template = ResumeTemplate(name=name, content=content, is_default=set_default or is_first)
    db.add(template)
    db.flush()
    if template.is_default:
        _ensure_single_default(db, template.id)
    db.commit()
    db.refresh(template)
    return template


def update_template_content(db: Session, template_id: int, name: str | None = None, content: str | None = None) -> ResumeTemplate:
    template = get_template(db, template_id)
    if content is not None:
        if not content.strip():
            raise ValueError("模板内容不能为空")
        validate_template_renders(content)
        template.content = content
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("模板名称不能为空")
        template.name = name
    db.commit()
    db.refresh(template)
    return template


def set_default_template(db: Session, template_id: int) -> ResumeTemplate:
    template = get_template(db, template_id)
    template.is_default = True
    _ensure_single_default(db, template.id)
    db.commit()
    db.refresh(template)
    return template


def delete_template(db: Session, template_id: int) -> None:
    template = get_template(db, template_id)
    total = db.query(ResumeTemplate).count()
    if total <= 1:
        raise ValueError("至少要保留一个 MD 模板，不能把最后一个删掉")
    was_default = template.is_default
    db.delete(template)
    db.flush()
    if was_default:
        # 删掉的正好是默认模板：随便挑剩下的一个顶上，不能让库里变成
        # "一个默认都没有"的状态（get_or_create_default_template 虽然
        # 也能兜底补一个新的,但那样会凭空多出一份和用户预期不符的模板）。
        promoted = db.query(ResumeTemplate).order_by(ResumeTemplate.id).first()
        if promoted is not None:
            promoted.is_default = True
    db.commit()
