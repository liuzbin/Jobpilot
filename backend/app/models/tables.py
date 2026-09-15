"""
六张核心表的 SQLAlchemy 模型定义，对应实施方案"三、数据模型设计"一节。

当前是单画像方案（MVP 阶段用户确认：一台电脑一份画像），所以这些表都不带
profile_id 维度；后续如果要支持多画像，再统一加这个字段做迁移。
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Enum,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.db import Base


class ExperienceLevel(str, enum.Enum):
    COMPANY = "company"  # A 层：公司
    POSITION = "position"  # B 层：项目 + 岗位 + 时间


class JDStatus(str, enum.Enum):
    PENDING = "pending"  # 未分析
    ANALYZING = "analyzing"  # 正在分析
    ANALYZED = "analyzed"  # 已分析
    TAILORED = "tailored"  # 已定制简历
    APPLIED = "applied"  # 已投递


class QASource(str, enum.Enum):
    ONBOARDING = "onboarding"  # 建画像阶段主动收集
    APPLICATION = "application"  # 某次投递过程中新遇到并补充


class ModelSlot(str, enum.Enum):
    LIGHT = "light"  # 轻量模型槽位
    HEAVY = "heavy"  # 重量模型槽位


class ProfileBasic(Base):
    """基本信息表，单行记录（id 恒为 1）。"""

    __tablename__ = "profile_basic"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    full_name: Mapped[str | None] = mapped_column(String(200))
    target_title: Mapped[str | None] = mapped_column(String(200))
    education: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(200))
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    github_url: Mapped[str | None] = mapped_column(String(500))
    school: Mapped[str | None] = mapped_column(String(200))
    years_experience: Mapped[float | None] = mapped_column(Float)
    current_location: Mapped[str | None] = mapped_column(String(200))
    target_location: Mapped[str | None] = mapped_column(String(200))
    work_authorization: Mapped[str | None] = mapped_column(String(200))
    # 打磨阶段后新增：MD 简历模板需要的"个人总结"/"技能"两块背景信息——
    # 这两块不参与 JD 关键词匹配/K 值裁剪逻辑（不是某段具体经历的贡献句，
    # 没有"命中/未命中"这个概念），每次生成简历都原样带上。存储成多行
    # 纯文本，一行一条，对应 Dashboard 上的一个 textarea：这样写和读都不需要
    # 额外的结构化解析，用户自己想怎么分段都行，简历模板渲染时按行拆成列表
    # 逐行输出（见 resume_tailor.build_static_resume_context）。
    resume_summary: Mapped[str | None] = mapped_column(Text)
    skills_text: Mapped[str | None] = mapped_column(Text)
    # LinkedIn 画像功能新增：给 LLM 的附加信息（自我评价的优势/劣势、求职
    # 偏好等），不是某段具体经历的事实描述，所以刻意不放进 BASIC_FIELDS
    # 里那些"简历会原样带上"的静态背景字段——这一条明确只喂给
    # app.services.scoring 的 JD 匹配打分 LLM 调用做语义参考（体现在
    # strengths/weaknesses 里），不会出现在任何生成出来的简历正文中。
    # 走和 resume_summary/skills_text 一样的"表单整体覆盖"逻辑，只是
    # 单独开一个小表单提交（见 routes_dashboard.profile_update_notes），
    # 避免和主表单混在一起时不小心被空值覆盖掉。
    additional_notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class ExperienceEntry(Base):
    """工作经历树：A 层公司 / B 层项目+岗位+时间，用 parent_id 自关联。"""

    __tablename__ = "experience_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("experience_entry.id", ondelete="CASCADE")
    )
    level: Mapped[ExperienceLevel] = mapped_column(Enum(ExperienceLevel), nullable=False)

    # level == COMPANY 时使用
    company_name: Mapped[str | None] = mapped_column(String(300))

    # level == POSITION 时使用
    position_title: Mapped[str | None] = mapped_column(String(300))
    project_name: Mapped[str | None] = mapped_column(String(300))
    start_date: Mapped[str | None] = mapped_column(String(20))  # "2022-06" 这种粒度即可
    end_date: Mapped[str | None] = mapped_column(String(20))
    is_current: Mapped[bool] = mapped_column(default=False)

    # Phase 2 新增：项目背景与理解（见实施方案 4/5.1）。不是让用户对着空文本框
    # 写作文，而是通过"追问式访谈"逐步填充——background_qa 存原始问答，
    # background_notes 存系统整理出的连贯文本，是简历重制阶段判断"某个
    # JD 要求的技能和这个项目的技术背景是否相关"时的主要依据。
    background_notes: Mapped[str | None] = mapped_column(Text)
    # [{"question": "...", "answer": "..."}, ...]，按访谈轮次追加
    background_qa: Mapped[list | None] = mapped_column(JSON)

    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    children: Mapped[list["ExperienceEntry"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    parent: Mapped["ExperienceEntry"] = relationship(
        back_populates="children", remote_side=[id]
    )
    bullets: Mapped[list["ExperienceBullet"]] = relationship(
        back_populates="position", cascade="all, delete-orphan"
    )
    claimed_skills: Mapped[list["ClaimedSkill"]] = relationship(
        back_populates="experience_entry", cascade="all, delete-orphan"
    )


class ExperienceBullet(Base):
    """C 层：具体贡献句，挂在某个 B 层（position）条目下,带标签冗余存储 A/B 层上下文。"""

    __tablename__ = "experience_bullet"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(
        ForeignKey("experience_entry.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # tags 冗余存储 [company_name, position_title, project_name]，
    # 目的是单独把这一句话喂给 LLM 时,不必再反查父级也能带上完整上下文
    tags: Mapped[list | None] = mapped_column(JSON)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)

    # Phase 2 新增：从 content 抽取出的"关键词 + 行为 + 结果"三元组衍生字段
    # （见实施方案 4/5.1）。content 本身不变、永远是唯一的事实来源；这三个
    # 字段只是从它提炼出来、用于简历重制阶段做关键词匹配和内容重组的索引。
    keywords: Mapped[list | None] = mapped_column(JSON)  # ["Spark", "ETL", ...]
    action_summary: Mapped[str | None] = mapped_column(Text)
    result_summary: Mapped[str | None] = mapped_column(Text)

    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    position: Mapped[ExperienceEntry] = relationship(back_populates="bullets")


class EducationEntry(Base):
    """教育经历（打磨阶段后新增）：MD 简历模板里的 EDUCATION 一节需要能列出
    多条学历（比如硕士+本科），`profile_basic.education/school` 这两个单值
    字段不够用，这里单独开一张表。和公司经历不同，教育经历基本不会有措辞
    分歧需要冲突确认，合并逻辑上按"学校+学位标准化后完全一致"做精确去重
    就够用（见 profile_service.merge_parsed_experience），不做模糊匹配。"""

    __tablename__ = "education_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    school: Mapped[str | None] = mapped_column(String(300))
    degree: Mapped[str | None] = mapped_column(String(300))
    location: Mapped[str | None] = mapped_column(String(300))
    start_date: Mapped[str | None] = mapped_column(String(20))
    end_date: Mapped[str | None] = mapped_column(String(20))
    is_current: Mapped[bool] = mapped_column(default=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class PersonalProject(Base):
    """独立项目（打磨阶段后新增）：不挂靠任何公司的个人/课外项目，对应 MD
    简历模板里独立于工作经历之外的 PROJECT EXPERIENCE 一节。结构上刻意做成
    和 experience_entry 的 B 层（position）平行但不复用同一张表——这类项目
    没有"公司"这个上一级，也不需要 A/B/C 三层里 A 层那套精确匹配逻辑,复用
    反而会让 experience_entry 的 level 语义变得混乱。

    这一版明确不参与 JD 打分（scoring.build_profile_context）、也不受 K 值
    控制（resume_tailor.select_keywords_to_extend 完全不碰这张表）——和
    profile_basic.resume_summary/skills_text 一样，当成"静态背景信息"。这
    条边界没有变；但简历重制阶段的"品控"（打磨阶段用户反馈第 3 点）新增了
    一层纯展示层面的筛选：生成简历时只挑对当前 JD 相关度最高的 2 个项目、
    每个最多展示 2 条 bullet（resume_tailor._select_top_projects），不是
    "每次都原样带上全部内容"了——这个筛选只影响某一次生成出来的简历长什么
    样，不修改这张表本身的任何数据，画像页的项目管理功能仍然能看到和编辑
    全部项目。详见 docs/DEVELOPMENT_LOG.md 对应章节。"""

    __tablename__ = "personal_project"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_name: Mapped[str] = mapped_column(String(300), nullable=False)
    start_date: Mapped[str | None] = mapped_column(String(20))
    end_date: Mapped[str | None] = mapped_column(String(20))
    is_current: Mapped[bool] = mapped_column(default=False)
    # LinkedIn 画像功能新增：自由文本标签，用来和 experience_entry 里的某家
    # 公司做人工关联展示（比如这个独立项目其实是在某家公司实习期间做的）。
    # 刻意只在这一侧加字段，不改 experience_entry、不建外键——见实施方案
    # 对应章节：两边各自保留独立的描述文本，这个标签只是方便用户在画像页
    # 上一眼看出"这个项目和那段工作经历是同一件事"，不参与任何合并/打分/
    # 简历生成逻辑。
    company_tag: Mapped[str | None] = mapped_column(String(300))
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    bullets: Mapped[list["PersonalProjectBullet"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class PersonalProjectBullet(Base):
    """独立项目下的贡献句，结构上比 experience_bullet 简单——独立项目不参与
    关键词匹配/延伸建议,所以不需要 tags/keywords/action_summary/result_summary
    这些衍生字段,content 就是最终展示的完整文本。"""

    __tablename__ = "personal_project_bullet"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("personal_project.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    project: Mapped[PersonalProject] = relationship(back_populates="bullets")


class ProfileSkill(Base):
    """结构化技能列表（LinkedIn 画像功能新增）：对照 LinkedIn Skills 板块的
    扁平标签结构，每条就是一个技能名，不做分类。单独开一张表而不是塞进
    `profile_basic.skills_text` 那段自由文本里，是因为这批数据主要来源于
    LinkedIn 页面抓取——抓取本身就是一份份离散的技能标签，不需要（也不
    应该）再喂给 LLM 做"分段/分类"这种重活；合并新抓取的技能时按技能名
    精确去重（大小写不敏感）就够用，和 education_entry/personal_project
    的合并逻辑是一个路数,成本上比每次都要 LLM 去理解/重排一段自由文本低
    得多。

    `resume_summary`/`skills_text` 这批"简历上传解析出的自由文本背景信息"
    不受影响、继续保留；生成简历时这张表的内容作为补充追加在 skills_text
    派生的技能行之后（见 resume_tailor._build_skills_lines），不会覆盖
    原有内容，也不会和已经出现在 skills_text 里的技能重复列出。"""

    __tablename__ = "profile_skill"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_name: Mapped[str] = mapped_column(String(200), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ResumeTemplate(Base):
    """MD 简历模板库（打磨阶段后新增，见实施方案对应章节）：`content` 是一段
    带 Jinja2 占位符的 Markdown 源文本，渲染时喂给
    `resume_template_service.render_template_markdown`，上下文结构见该模块
    文档字符串。允许多个模板共存,`is_default` 恒有且只有一条为 True——
    这条不变量由 resume_template_service 的写入逻辑维护，不是数据库约束
    （sqlite 的部分唯一索引写法比较别扭，用代码保证更直接，也方便测试锁定）。

    "内置样式"（default/compact，见 resume_pdf.AVAILABLE_RESUME_STYLES）
    完全独立于这张表，两套机制并存、互不影响——这是打磨阶段用户反馈明确
    要求"两套都留着，用户可选"的结果。"""

    __tablename__ = "resume_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class JDRecord(Base):
    """JD 记录表：用户手动粘贴的岗位信息 + 解析后的结构化字段 + 投递状态机。"""

    __tablename__ = "jd_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company: Mapped[str | None] = mapped_column(String(300))
    title: Mapped[str | None] = mapped_column(String(300))
    description_raw: Mapped[str] = mapped_column(Text, nullable=False)  # JD 原文全文
    location: Mapped[str | None] = mapped_column(String(300))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    # "Toronto, ON · 2 weeks ago · 80 people clicked apply ..." 这类附加信息原文
    extra_meta_raw: Mapped[str | None] = mapped_column(Text)
    # 解析后的结构化字段：posted_ago, applicants_clicked, is_promoted,
    # responses_managed_off_linkedin, required_years, required_education,
    # required_clearance, plus_skills[] 等,具体键值在 JD 解析模块里定义
    parsed_meta: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[JDStatus] = mapped_column(
        Enum(JDStatus), default=JDStatus.PENDING, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    scores: Mapped[list["MatchScore"]] = relationship(
        back_populates="jd", cascade="all, delete-orphan"
    )
    resume_versions: Mapped[list["ResumeVersion"]] = relationship(
        back_populates="jd", cascade="all, delete-orphan"
    )


class MatchScore(Base):
    """打分结果表：维度分数 + 加减分明细 + 优劣势,全部结构化存储便于复现和排查。"""

    __tablename__ = "match_score"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jd_id: Mapped[int] = mapped_column(
        ForeignKey("jd_record.id", ondelete="CASCADE"), nullable=False
    )
    skill_fit_score: Mapped[float] = mapped_column(Float, nullable=False)  # 维度一 0-100
    hard_requirement_penalty: Mapped[float] = mapped_column(Float, default=0)  # 维度二扣分（负数或0）
    flexible_adjustment: Mapped[float] = mapped_column(Float, default=0)  # 维度三 加减分合计
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    # breakdown: 每一条扣分/加分的具体理由，例如
    # [{"type": "years_gap", "detail": "要求5年,画像3年,差2年", "delta": -10}, ...]
    breakdown: Mapped[list | None] = mapped_column(JSON)
    strengths: Mapped[list | None] = mapped_column(JSON)
    weaknesses: Mapped[list | None] = mapped_column(JSON)
    model_used: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    jd: Mapped[JDRecord] = relationship(back_populates="scores")


class ResumeVersion(Base):
    """简历重制版本表：同一个 JD 可以对应多个不同 K 值/风格的版本。"""

    __tablename__ = "resume_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jd_id: Mapped[int] = mapped_column(
        ForeignKey("jd_record.id", ondelete="CASCADE"), nullable=False
    )
    k_value: Mapped[int] = mapped_column(Integer, nullable=False)  # 0-10
    style_id: Mapped[str] = mapped_column(String(100), default="default")
    # 打磨阶段后新增：非空时代表这个版本用的是 MD 模板库里的某个模板渲染的，
    # 此时 style_id 恒存一个固定哨兵值（resume_pdf.MD_TEMPLATE_STYLE_SENTINEL），
    # 渲染走 resume_template_service 那条路径；为 None 时完全是旧行为，走
    # style_id 对应的内置 CSS 模板（resume_pdf.AVAILABLE_RESUME_STYLES）。
    # 模板被删除不应该让历史简历版本报错，所以用 SET NULL 而不是 CASCADE——
    # 只是没法再用"换个 MD 模板重新渲染"这个操作了，已经生成好的 PDF/
    # markdown_text 不受影响。
    resume_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_template.id", ondelete="SET NULL")
    )
    resume_json: Mapped[dict | None] = mapped_column(JSON)
    markdown_text: Mapped[str | None] = mapped_column(Text)
    pdf_path: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    jd: Mapped[JDRecord] = relationship(back_populates="resume_versions")
    resume_template: Mapped["ResumeTemplate | None"] = relationship()


class QABankEntry(Base):
    """投递表单主观题题库：问题 + embedding + 答案，供相似度检索复用。"""

    __tablename__ = "qa_bank"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[QASource] = mapped_column(Enum(QASource), nullable=False)
    jd_id: Mapped[int | None] = mapped_column(
        ForeignKey("jd_record.id", ondelete="SET NULL")
    )
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class ClaimedSkill(Base):
    """认领技能库（Phase 2 新增，见实施方案 4/5.3）：用户在简历重制时对"延伸建议"
    逐条确认接受之后，持久化在这里，供以后别的 JD 复用。每一条都是一套完整的
    "关键词 + 行为 + 结果"三元组模板，而不只是一个技能名——单独一个技能名没法
    直接用于写简历，也没法在面试里讲清楚。

    这张表的内容明确不参与 app.services.scoring.compute_score 的打分计算：
    打分要对用户保持诚实，不能因为"认领了"某项技能就让匹配分数上涨，否则
    "帮用户看清楚自己和市场的真实差距"这条核心价值就无从谈起（见实施方案
    第一节"核心价值主张"）。
    """

    __tablename__ = "claimed_skill"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_name: Mapped[str] = mapped_column(String(200), nullable=False)
    experience_entry_id: Mapped[int] = mapped_column(
        ForeignKey("experience_entry.id", ondelete="CASCADE"), nullable=False
    )
    action_summary: Mapped[str] = mapped_column(Text, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text)
    # 为什么认为这项技能和这个项目的技术背景相关（呈现给用户确认时的依据）
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    # 第一次触发这条建议的 JD，JD 被删除不影响这条认领记录本身
    source_jd_id: Mapped[int | None] = mapped_column(
        ForeignKey("jd_record.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    experience_entry: Mapped[ExperienceEntry] = relationship(back_populates="claimed_skills")


class LLMUsageLog(Base):
    """Phase 5"用量统计面板"：每次调用轻量/重量模型槽位都记一条，无论成功
    还是失败。只统计调用次数和 token 数（`prompt_tokens`/`completion_tokens`/
    `total_tokens` 来自模型返回的 OpenAI 兼容 `usage` 字段，不是所有网关都会
    返回，缺失时就是 NULL），不做费用估算——用户各自配置的模型服务定价不
    统一（不同厂商、不同套餐、汇率都不一样），这不是本项目能可靠获取的信息，
    硬凑一个金额出来只会是一种误导性的伪精确，不如老老实实只展示调用次数
    和 token 数,让用户自己对照自己那边的账单。"""

    __tablename__ = "llm_usage_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slot: Mapped[ModelSlot] = mapped_column(Enum(ModelSlot), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(200))
    ok: Mapped[bool] = mapped_column(nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ModelConfig(Base):
    """轻量/重量两个模型槽位的配置。API Key 不落库,只存 keyring 引用名。"""

    __tablename__ = "model_config"
    __table_args__ = (UniqueConstraint("slot", name="uq_model_config_slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slot: Mapped[ModelSlot] = mapped_column(Enum(ModelSlot), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500))
    model_name: Mapped[str | None] = mapped_column(String(200))
    keyring_ref: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
