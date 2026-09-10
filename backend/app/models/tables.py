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
    resume_json: Mapped[dict | None] = mapped_column(JSON)
    markdown_text: Mapped[str | None] = mapped_column(Text)
    pdf_path: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    jd: Mapped[JDRecord] = relationship(back_populates="resume_versions")


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
