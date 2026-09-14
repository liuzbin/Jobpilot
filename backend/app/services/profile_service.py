"""
用户画像的读写逻辑：基本信息表单 CRUD + 工作经历树（A/B/C 三层）的合并写入
与手动增删改。

关于"合并"的范围说明（Phase 1 曾经的一处有意简化，Phase 2 补完时按下面的
规则收窄了简化的范围，打磨阶段又把"职位"这一级的匹配从精确匹配扩展成了
模糊匹配，都记入 docs/DEVELOPMENT_LOG.md）：
- 公司按名称做不区分大小写的精确匹配，匹配上了就复用，匹配不上就新增；这一
  条不涉及信息丢失，继续自动处理，不用打扰用户。
- 职位（公司下的项目+岗位+时间区间）优先按"标题+项目名标准化后完全一致"精确
  匹配；精确匹配不到时，再退一步做模糊匹配：同一家公司下，起止时间有重叠
  （或双方有一方压根没填时间，没法比较）、且岗位名称/项目名称的文本相似度
  达到阈值（复用下面贡献句去重同一套 difflib 相似度算法和阈值），判定为
  "很可能是同一段经历的不同措辞"——这种情况不当成一条新职位插入，而是和
  起止时间不一致的处理方式一样，把"职位名称"/"项目名称"这两个字段的差异
  也作为 position_field_conflicts 交给用户确认，不默默采用新值覆盖旧值，也
  不默默丢弃、更不会当成两条独立的职位都保留下来（会导致画像里出现大量
  看起来是同一段经历的重复条目）。模糊匹配也找不到候选，才真的当成一段新
  职位插入。这条设计明确不用 LLM 判断"是不是同一段经历"——公司名+时间+
  标题相似度这几个信号足够靠工具确定性地判断，没必要为这么一个判断专门
  再多一次 LLM 调用。
- 贡献句按去除首尾空白后的精确文本去重，已存在的句子不重复插入；这一条同样
  不用打扰用户。
- 贡献句如果不是精确重复、但和同一段职位下已有的某条贡献句高度相似（用
  difflib 算字符串相似度），说明很可能是同一件事的不同措辞，不能默默地
  当成两条不相关的贡献句自动都加进去（会导致画像里出现大量看起来重复的
  条目），也不能默默丢弃新的那条（可能新措辞更准确、更完整）——这种情况下
  交给用户选择：保留旧的/替换成新的/两条都要。
- 职位已经存在（不管是精确匹配还是模糊匹配到的）、但重新解析出的起止时间或
  "是否至今"和已有记录不一致时，同样不默默用新值覆盖旧值（有可能新解析反而
  不准），交给用户确认。
- 基本信息表单本身走"用户显式编辑就整体覆盖"的逻辑（Dashboard 表单提交视为
  用户已经确认过内容）；但简历上传自动抽取出的基本信息只用来"填补空白字段",
  不会覆盖用户已经手动填过的字段,避免解析误差覆盖掉用户确认过的准确信息。

除此之外，本文件也提供工作经历树的手动增删改：新增公司/职位/贡献句、编辑
职位字段、编辑贡献句内容、删除公司/职位/贡献句——Phase 1 只能靠重新上传
简历增量补充，画像里的错误没法直接改，这里补上直接编辑的入口。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.tables import (
    EducationEntry,
    ExperienceBullet,
    ExperienceEntry,
    ExperienceLevel,
    PersonalProject,
    PersonalProjectBullet,
    ProfileBasic,
)

# 贡献句相似度阈值：高于这个值判定为"很可能是同一件事的不同措辞"，需要用户
# 确认怎么处理；低于这个值就当成两条不相关的贡献句直接都保留。取 0.82 是
# 观察典型改写（换几个词、调整语序）之后的经验值——既能抓住"基本是同一句话
# 微调措辞"的情况，也不会把两条主题相关但确实是不同事情的贡献句误判成冲突。
_BULLET_SIMILARITY_THRESHOLD = 0.82

# 职位名称/项目名称模糊匹配同样复用这个阈值——目的是同一件事("这段文本改写
# 前后是不是本质上说的同一件事")，没有理由用两套不同的判定口径。
_POSITION_TITLE_SIMILARITY_THRESHOLD = _BULLET_SIMILARITY_THRESHOLD

_YEAR_MONTH_RE = re.compile(r"^(\d{4})(?:-(\d{1,2}))?$")
# "至今"在时间轴上当成一个足够大的月份序数,保证一定不会被判定成"在任何
# 已经解析出来的历史时间段之前结束"。
_ONGOING_MONTH_ORDINAL = 999912

BASIC_FIELDS = [
    "full_name",
    "target_title",
    "education",
    "phone",
    "email",
    "linkedin_url",
    "github_url",
    "school",
    "years_experience",
    "current_location",
    "target_location",
    "work_authorization",
    # 打磨阶段后新增，见 ExperienceEntry/ProfileBasic 表注释。这两个字段
    # 存成多行纯文本（一条一行），和其它 BASIC_FIELDS 走一模一样的"表单
    # 提交整体覆盖 / 简历解析只填空字段"逻辑,不需要单独的合并函数。
    "resume_summary",
    "skills_text",
]


def get_or_create_profile_basic(db: Session) -> ProfileBasic:
    row = db.get(ProfileBasic, 1)
    if row is None:
        row = ProfileBasic(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def update_profile_basic(db: Session, fields_in: dict) -> ProfileBasic:
    """Dashboard 表单提交走这条路径：只更新传入的字段,视为用户显式确认过的值,直接覆盖。"""
    row = get_or_create_profile_basic(db)
    for key in BASIC_FIELDS:
        if key in fields_in:
            setattr(row, key, fields_in[key])
    db.commit()
    db.refresh(row)
    return row


def fill_blank_profile_basic_fields(db: Session, extracted: dict) -> list[str]:
    """简历自动抽取走这条路径：只填空字段,已有值不覆盖。返回被填充的字段名列表。"""
    row = get_or_create_profile_basic(db)
    filled = []
    for key in BASIC_FIELDS:
        current = getattr(row, key, None)
        if (current is None or current == "") and extracted.get(key):
            setattr(row, key, extracted[key])
            filled.append(key)
    if filled:
        db.commit()
        db.refresh(row)
    return filled


def get_experience_tree(db: Session) -> list[dict]:
    """返回嵌套结构，供 Dashboard 渲染和喂给 LLM 用：
    [{company_name, id, positions: [{id, position_title, project_name, start_date,
      end_date, is_current, bullets: [{id, content, tags}]}]}]
    """
    companies = (
        db.query(ExperienceEntry)
        .filter(ExperienceEntry.level == ExperienceLevel.COMPANY)
        .order_by(ExperienceEntry.order_index, ExperienceEntry.id)
        .all()
    )
    result = []
    for company in companies:
        positions = []
        for pos in sorted(company.children, key=lambda p: (p.order_index, p.id)):
            bullets = sorted(pos.bullets, key=lambda b: (b.order_index, b.id))
            positions.append(
                {
                    "id": pos.id,
                    "position_title": pos.position_title,
                    "project_name": pos.project_name,
                    "start_date": pos.start_date,
                    "end_date": pos.end_date,
                    "is_current": pos.is_current,
                    "bullets": [{"id": b.id, "content": b.content, "tags": b.tags} for b in bullets],
                }
            )
        result.append({"id": company.id, "company_name": company.company_name, "positions": positions})
    return result


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


@dataclass
class MergeResult:
    companies_added: int = 0
    positions_added: int = 0
    bullets_added: int = 0
    bullets_skipped_duplicate: int = 0
    basic_fields_filled: list[str] = field(default_factory=list)
    # 下面两项是"待用户确认"的冲突，不会自动应用，调用方需要在 Dashboard 上
    # 展示出来，收集用户选择后调用 resolve_bullet_conflict / resolve_position_field
    # 才会真正生效。每一项都是普通 dict（不是 dataclass），方便直接序列化成
    # JSON 在 GET 渲染的表单和 POST 提交之间往返（做法上和 resume_tailor 的
    # hit_items_json 是同一个模式）。
    bullet_conflicts: list[dict] = field(default_factory=list)
    position_field_conflicts: list[dict] = field(default_factory=list)
    # 打磨阶段后新增：教育经历/独立项目这两块"静态背景信息"的合并统计，
    # 语义上不算"冲突"（不需要用户确认），所以没有对应的 conflicts 列表——
    # 具体规则见 _merge_education_entries/_merge_personal_projects 的注释。
    # 个人总结/技能这两个字段现在直接走 BASIC_FIELDS 那套"只填空"逻辑，
    # 已经体现在 basic_fields_filled 里，不需要单独的统计字段。
    education_added: int = 0
    projects_added: int = 0
    project_bullets_added: int = 0


def _position_label(position: ExperienceEntry) -> str:
    return " / ".join(filter(None, [position.position_title, position.project_name])) or "（未命名职位）"


def _check_position_field_conflicts(position: ExperienceEntry, position_in: dict) -> list[dict]:
    """职位已存在时（不管是精确匹配还是模糊匹配到的），检查岗位名称/项目
    名称/起止时间/是否至今是否和新解析出的值冲突。只有"已有值非空、新值也
    非空、且两者不同"才算冲突；已有值本来就是空的，直接当填空处理（不算
    冲突，也不用打扰用户）。

    岗位名称/项目名称这两项冲突主要来自模糊匹配到的职位——精确匹配的定义
    就是这两个字段标准化后完全一致，走精确匹配的职位不会触发这里的冲突；
    只有模糊匹配（时间重叠/一方缺失 + 文本相似度达标）找到的职位，新解析出
    的标题/项目名文本和已有记录不完全一样，才需要用户确认到底以哪个措辞
    为准，而不是默默保留旧的或者默默改成新的。"""
    conflicts: list[dict] = []
    company_name = position.parent.company_name if position.parent else None
    label = _position_label(position)

    for field_name in ("position_title", "project_name"):
        incoming = (position_in.get(field_name) or "").strip() or None
        current = getattr(position, field_name)
        if incoming is None:
            continue
        if current is None:
            setattr(position, field_name, incoming)
            continue
        if _norm(current) == _norm(incoming):
            continue
        conflicts.append(
            {
                "position_id": position.id,
                "company_name": company_name,
                "position_label": label,
                "field": field_name,
                "old_value": current,
                "new_value": incoming,
            }
        )

    for field_name in ("start_date", "end_date"):
        incoming = (position_in.get(field_name) or "").strip() or None
        current = position.start_date if field_name == "start_date" else position.end_date
        if incoming is None:
            continue
        if current is None:
            setattr(position, field_name, incoming)
            continue
        if str(current).strip() == incoming:
            continue
        conflicts.append(
            {
                "position_id": position.id,
                "company_name": company_name,
                "position_label": label,
                "field": field_name,
                "old_value": current,
                "new_value": incoming,
            }
        )

    incoming_is_current = bool(position_in.get("is_current", False))
    if incoming_is_current and not position.is_current:
        conflicts.append(
            {
                "position_id": position.id,
                "company_name": company_name,
                "position_label": label,
                "field": "is_current",
                "old_value": position.is_current,
                "new_value": True,
            }
        )
    return conflicts


def _parse_month_ordinal(value) -> int | None:
    """把 "YYYY-MM"/"YYYY" 解析成一个可比较大小的整数（year*12+month，月份
    缺失按 1 月算），方便判断两段时间是否重叠；解析不出来（格式不规范、
    为空、None）统一返回 None。"""
    if not value:
        return None
    match = _YEAR_MONTH_RE.match(str(value).strip())
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else 1
    return year * 12 + month


def _date_range_bounds(start, end, is_current: bool) -> tuple[int | None, int | None] | None:
    """返回 (起始月份序数, 结束月份序数)；两边都解析不出来时返回 None，代表
    "这一方压根没填时间"，调用方应该跳过时间重叠判断，不能强行比较。结束
    时间为空但标了"至今"，按一个足够大的序数处理，保证一定不会被判定成
    "早于任何已经解析出来的历史时间段就结束了"。"""
    start_ord = _parse_month_ordinal(start)
    end_ord = _parse_month_ordinal(end)
    if start_ord is None and end_ord is None:
        return None
    if end_ord is None and is_current:
        end_ord = _ONGOING_MONTH_ORDINAL
    return (start_ord, end_ord)


def _date_ranges_overlap_or_missing(existing: ExperienceEntry, position_in: dict) -> bool:
    """判断"已有职位"和"新解析出的职位"这两段时间是否重叠，或者其中一方
    压根没填时间（没法比较，不应该仅凭这一点就拒绝模糊匹配，交给标题/项目
    名相似度去判断）。只有双方都确实填了时间、且时间对不上（一段早就结束
    了另一段才开始）才返回 False。"""
    existing_range = _date_range_bounds(existing.start_date, existing.end_date, existing.is_current)
    incoming_range = _date_range_bounds(
        position_in.get("start_date"), position_in.get("end_date"), bool(position_in.get("is_current", False))
    )
    if existing_range is None or incoming_range is None:
        return True

    def _fill(range_: tuple[int | None, int | None], default_start: int, default_end: int) -> tuple[int, int]:
        start_ord, end_ord = range_
        return (start_ord if start_ord is not None else default_start, end_ord if end_ord is not None else default_end)

    e_start, e_end = _fill(existing_range, -_ONGOING_MONTH_ORDINAL, _ONGOING_MONTH_ORDINAL)
    i_start, i_end = _fill(incoming_range, -_ONGOING_MONTH_ORDINAL, _ONGOING_MONTH_ORDINAL)
    return e_start <= i_end and i_start <= e_end


def _position_similarity(existing: ExperienceEntry, position_in: dict) -> float:
    """岗位名称、项目名称两个字段分别算相似度，取较高的一个——只要有一个
    字段的措辞对得上（比如岗位名称完全没变，只是项目名称换了个更具体的
    叫法），就有理由怀疑是同一段经历，不要求两个字段同时达标。"""
    title_ratio = difflib.SequenceMatcher(
        None, _norm(existing.position_title), _norm(position_in.get("position_title"))
    ).ratio()
    project_ratio = difflib.SequenceMatcher(
        None, _norm(existing.project_name), _norm(position_in.get("project_name"))
    ).ratio()
    return max(title_ratio, project_ratio)


def _find_matching_position(
    candidates: list[ExperienceEntry], position_in: dict, exact_index: dict[tuple[str, str], ExperienceEntry]
) -> ExperienceEntry | None:
    """在同一家公司下找"新解析出的这段职位"对应的已有职位。优先走精确匹配
    （标题+项目名标准化后完全一致，和 Phase 2 之前的行为完全一样，不需要
    用户确认）；精确匹配不到，再退一步模糊匹配：时间重叠或有一方缺失、且
    标题/项目名相似度达到阈值，取相似度最高的一个候选，判定为"很可能是
    同一段经历的不同措辞"。两种都匹配不到才返回 None（当成新职位插入）。
    """
    position_title = (position_in.get("position_title") or "").strip()
    project_name = (position_in.get("project_name") or "").strip()
    exact = exact_index.get((_norm(position_title), _norm(project_name)))
    if exact is not None:
        return exact

    best_candidate: ExperienceEntry | None = None
    best_ratio = 0.0
    for candidate in candidates:
        if not _date_ranges_overlap_or_missing(candidate, position_in):
            continue
        ratio = _position_similarity(candidate, position_in)
        if ratio >= _POSITION_TITLE_SIMILARITY_THRESHOLD and ratio > best_ratio:
            best_candidate = candidate
            best_ratio = ratio
    return best_candidate


_MONTH_DISPLAY_NAMES = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def format_date_range(start_date: str | None, end_date: str | None, is_current: bool) -> str:
    """把 "YYYY-MM"/"YYYY" 格式的起止时间拼成一段人可读的展示文本（比如
    "Feb 2025 – 至今"），给 MD 简历模板用（见 resume_tailor.build_resume_json
    里的 date_range 字段）——内置的 default/compact 两套 CSS 风格目前不需要
    这个，日期解析失败就原样展示原始字符串，不强行报错。"""

    def _display_one(value: str | None) -> str:
        if not value:
            return ""
        match = _YEAR_MONTH_RE.match(str(value).strip())
        if not match:
            return str(value).strip()
        year, month = match.group(1), match.group(2)
        if month and 1 <= int(month) <= 12:
            return f"{_MONTH_DISPLAY_NAMES[int(month)]} {year}"
        return year

    start_display = _display_one(start_date)
    end_display = "至今" if is_current else _display_one(end_date)
    if start_display and end_display:
        return f"{start_display} – {end_display}"
    return start_display or end_display


def _find_similar_bullet(position: ExperienceEntry, bullet_text: str) -> tuple[ExperienceBullet | None, float]:
    """在 position 已有的贡献句里找和 bullet_text 最相似的一条，返回
    (最相似的 bullet 或 None, 相似度)。"""
    best_bullet = None
    best_ratio = 0.0
    norm_text = _norm(bullet_text)
    for existing in position.bullets:
        ratio = difflib.SequenceMatcher(None, norm_text, _norm(existing.content)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_bullet = existing
    return best_bullet, best_ratio


def _merge_education_entries(db: Session, entries_in: list[dict]) -> int:
    """教育经历按"学校+学位标准化后完全一致"精确去重——教育经历条目数量
    通常很少（1~3 条），不太会出现措辞上的微妙差异需要模糊匹配/冲突确认，
    精确匹配已经够用，没必要复用职位合并那一整套模糊匹配机制。"""
    existing = db.query(EducationEntry).all()
    existing_keys = {(_norm(e.school), _norm(e.degree)) for e in existing}
    max_order = max([e.order_index for e in existing], default=-1)

    added = 0
    for entry_in in entries_in:
        school = (entry_in.get("school") or "").strip()
        if not school:
            continue
        key = (_norm(school), _norm(entry_in.get("degree")))
        if key in existing_keys:
            continue
        max_order += 1
        db.add(
            EducationEntry(
                school=school,
                degree=(entry_in.get("degree") or "").strip() or None,
                location=(entry_in.get("location") or "").strip() or None,
                start_date=entry_in.get("start_date"),
                end_date=entry_in.get("end_date"),
                is_current=bool(entry_in.get("is_current", False)),
                order_index=max_order,
            )
        )
        existing_keys.add(key)
        added += 1
    if added:
        db.commit()
    return added


def _merge_personal_projects(db: Session, projects_in: list[dict]) -> tuple[int, int]:
    """独立项目按项目名精确匹配（标准化后完全一致）合并——不像公司下的职位
    那样做时间重叠+相似度的模糊匹配，是刻意收窄的范围：独立项目数量通常
    很少，精确匹配已经能覆盖"重复上传同一份简历"这个最常见的场景，模糊
    匹配那一套机制这里暂时不需要（见 docs/DEVELOPMENT_LOG.md 对应章节）。
    贡献句按精确文本去重，逻辑和公司经历下的贡献句去重一致。"""
    existing = db.query(PersonalProject).all()
    existing_index = {_norm(p.project_name): p for p in existing}
    max_order = max([p.order_index for p in existing], default=-1)

    projects_added = 0
    bullets_added = 0
    for project_in in projects_in:
        project_name = (project_in.get("project_name") or "").strip()
        if not project_name:
            continue
        project = existing_index.get(_norm(project_name))
        if project is None:
            max_order += 1
            project = PersonalProject(
                project_name=project_name,
                start_date=project_in.get("start_date"),
                end_date=project_in.get("end_date"),
                is_current=bool(project_in.get("is_current", False)),
                order_index=max_order,
            )
            db.add(project)
            db.flush()
            existing_index[_norm(project_name)] = project
            projects_added += 1

        existing_bullet_texts = {_norm(b.content) for b in project.bullets}
        max_bullet_order = max([b.order_index for b in project.bullets], default=-1)
        for bullet_text in project_in.get("bullets", []):
            bullet_text = (bullet_text or "").strip()
            if not bullet_text or _norm(bullet_text) in existing_bullet_texts:
                continue
            max_bullet_order += 1
            db.add(PersonalProjectBullet(project_id=project.id, content=bullet_text, order_index=max_bullet_order))
            existing_bullet_texts.add(_norm(bullet_text))
            bullets_added += 1

    if projects_added or bullets_added:
        db.commit()
    return projects_added, bullets_added


def merge_parsed_experience(db: Session, parsed: dict) -> MergeResult:
    """parsed 的结构见本文件顶部注释。做自动合并写入,返回统计结果，包括需要
    用户确认才会生效的冲突列表（result.bullet_conflicts / position_field_conflicts）。"""
    result = MergeResult()

    if parsed.get("basic"):
        result.basic_fields_filled = fill_blank_profile_basic_fields(db, parsed["basic"])

    if parsed.get("education_entries"):
        result.education_added = _merge_education_entries(db, parsed["education_entries"])

    if parsed.get("projects"):
        result.projects_added, result.project_bullets_added = _merge_personal_projects(db, parsed["projects"])

    existing_companies = (
        db.query(ExperienceEntry).filter(ExperienceEntry.level == ExperienceLevel.COMPANY).all()
    )
    company_index = {_norm(c.company_name): c for c in existing_companies}

    max_company_order = max([c.order_index for c in existing_companies], default=-1)

    for company_in in parsed.get("companies", []):
        company_name = (company_in.get("company_name") or "").strip()
        if not company_name:
            continue
        company = company_index.get(_norm(company_name))
        if company is None:
            max_company_order += 1
            company = ExperienceEntry(
                level=ExperienceLevel.COMPANY, company_name=company_name, order_index=max_company_order
            )
            db.add(company)
            db.flush()  # 拿到 id,后面 position 要用 parent_id
            company_index[_norm(company_name)] = company
            result.companies_added += 1

        # `existing_positions_exact` 只用来做精确匹配的 O(1) 查找；`position_candidates`
        # 是模糊匹配要扫描的候选列表——两个都手动维护、而不是直接用
        # `company.children`，是因为本轮循环里新建的职位是通过显式设置
        # `parent_id` 加进 session 的，不会自动出现在 `company.children`
        # 这个关系集合里，如果不手动同步，同一批上传里后面的职位就没法
        # 模糊匹配到前面刚插入的那个。
        existing_positions_exact = {
            (_norm(p.position_title), _norm(p.project_name)): p for p in company.children
        }
        position_candidates: list[ExperienceEntry] = list(company.children)
        max_position_order = max([p.order_index for p in company.children], default=-1)

        for position_in in company_in.get("positions", []):
            position_title = (position_in.get("position_title") or "").strip()
            project_name = (position_in.get("project_name") or "").strip()
            position = _find_matching_position(position_candidates, position_in, existing_positions_exact)
            if position is None:
                max_position_order += 1
                position = ExperienceEntry(
                    level=ExperienceLevel.POSITION,
                    parent_id=company.id,
                    position_title=position_title or None,
                    project_name=project_name or None,
                    start_date=position_in.get("start_date"),
                    end_date=position_in.get("end_date"),
                    is_current=bool(position_in.get("is_current", False)),
                    order_index=max_position_order,
                )
                db.add(position)
                db.flush()
                existing_positions_exact[(_norm(position_title), _norm(project_name))] = position
                position_candidates.append(position)
                result.positions_added += 1
            else:
                result.position_field_conflicts.extend(_check_position_field_conflicts(position, position_in))

            existing_bullet_texts = {_norm(b.content) for b in position.bullets}
            max_bullet_order = max([b.order_index for b in position.bullets], default=-1)
            tags = [company.company_name, position.position_title, position.project_name]
            for bullet_text in position_in.get("bullets", []):
                bullet_text = (bullet_text or "").strip()
                if not bullet_text:
                    continue
                if _norm(bullet_text) in existing_bullet_texts:
                    result.bullets_skipped_duplicate += 1
                    continue

                similar_bullet, ratio = _find_similar_bullet(position, bullet_text)
                if similar_bullet is not None and ratio >= _BULLET_SIMILARITY_THRESHOLD:
                    result.bullet_conflicts.append(
                        {
                            "position_id": position.id,
                            "company_name": company.company_name,
                            "position_label": _position_label(position),
                            "existing_bullet_id": similar_bullet.id,
                            "existing_text": similar_bullet.content,
                            "new_text": bullet_text,
                            "similarity": round(ratio, 3),
                        }
                    )
                    continue

                max_bullet_order += 1
                db.add(
                    ExperienceBullet(
                        position_id=position.id,
                        content=bullet_text,
                        tags=tags,
                        order_index=max_bullet_order,
                    )
                )
                existing_bullet_texts.add(_norm(bullet_text))
                result.bullets_added += 1

    db.commit()
    return result


# ---------- 合并冲突确认（用户在 Dashboard 上选完之后调用） ----------


def resolve_position_field(db: Session, position_id: int, field_name: str, new_value) -> None:
    position = db.get(ExperienceEntry, position_id)
    if position is None or position.level != ExperienceLevel.POSITION:
        return
    if field_name == "is_current":
        new_value = str(new_value).strip().lower() in ("true", "1", "on", "yes")
    elif field_name not in ("start_date", "end_date", "position_title", "project_name"):
        return
    setattr(position, field_name, new_value)
    db.commit()


def resolve_bullet_conflict(
    db: Session,
    position_id: int,
    choice: str,
    existing_bullet_id: int | None,
    new_text: str,
) -> None:
    """choice: "keep_old"（丢弃新句子，默认）/ "replace"（用新句子替换旧句子）/
    "keep_both"（两条都保留，当成不同的贡献句）。"""
    if choice == "replace" and existing_bullet_id:
        bullet = db.get(ExperienceBullet, existing_bullet_id)
        if bullet is not None and (new_text or "").strip():
            bullet.content = new_text.strip()
            # 内容变了，原来抽取的三元组不再可信，清空后下次 backfill 会重新抽取。
            bullet.keywords = None
            bullet.action_summary = None
            bullet.result_summary = None
            db.commit()
    elif choice == "keep_both":
        position = db.get(ExperienceEntry, position_id)
        new_text = (new_text or "").strip()
        if position is not None and new_text:
            max_order = max([b.order_index for b in position.bullets], default=-1)
            db.add(
                ExperienceBullet(
                    position_id=position_id,
                    content=new_text,
                    tags=[
                        position.parent.company_name if position.parent else None,
                        position.position_title,
                        position.project_name,
                    ],
                    order_index=max_order + 1,
                )
            )
            db.commit()
    # 其它取值（包括默认的 "keep_old"）：什么都不做，新句子被丢弃。


# ---------- 工作经历树的手动增删改 ----------


def _get_position(db: Session, position_id: int) -> ExperienceEntry:
    position = db.get(ExperienceEntry, position_id)
    if position is None or position.level != ExperienceLevel.POSITION:
        raise ValueError(f"职位不存在：id={position_id}")
    return position


def add_company(db: Session, company_name: str) -> ExperienceEntry:
    company_name = (company_name or "").strip()
    if not company_name:
        raise ValueError("公司名不能为空")
    existing = db.query(ExperienceEntry).filter(ExperienceEntry.level == ExperienceLevel.COMPANY).all()
    max_order = max([c.order_index for c in existing], default=-1)
    company = ExperienceEntry(level=ExperienceLevel.COMPANY, company_name=company_name, order_index=max_order + 1)
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


def delete_company(db: Session, company_id: int) -> bool:
    company = db.get(ExperienceEntry, company_id)
    if company is None or company.level != ExperienceLevel.COMPANY:
        return False
    db.delete(company)
    db.commit()
    return True


def add_position(
    db: Session,
    company_id: int,
    position_title: str | None = None,
    project_name: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    is_current: bool = False,
) -> ExperienceEntry:
    company = db.get(ExperienceEntry, company_id)
    if company is None or company.level != ExperienceLevel.COMPANY:
        raise ValueError(f"公司不存在：id={company_id}")
    max_order = max([p.order_index for p in company.children], default=-1)
    position = ExperienceEntry(
        level=ExperienceLevel.POSITION,
        parent_id=company_id,
        position_title=(position_title or "").strip() or None,
        project_name=(project_name or "").strip() or None,
        start_date=(start_date or "").strip() or None,
        end_date=(end_date or "").strip() or None,
        is_current=bool(is_current),
        order_index=max_order + 1,
    )
    db.add(position)
    db.commit()
    db.refresh(position)
    return position


def update_position_fields(db: Session, position_id: int, fields_in: dict) -> ExperienceEntry:
    position = _get_position(db, position_id)
    for key in ("position_title", "project_name", "start_date", "end_date"):
        if key in fields_in:
            value = (fields_in[key] or "").strip() or None
            setattr(position, key, value)
    if "is_current" in fields_in:
        position.is_current = bool(fields_in["is_current"])
    db.commit()
    db.refresh(position)
    return position


def delete_position(db: Session, position_id: int) -> bool:
    position = db.get(ExperienceEntry, position_id)
    if position is None or position.level != ExperienceLevel.POSITION:
        return False
    db.delete(position)
    db.commit()
    return True


def add_bullet(db: Session, position_id: int, content: str) -> ExperienceBullet:
    position = _get_position(db, position_id)
    content = (content or "").strip()
    if not content:
        raise ValueError("贡献句内容不能为空")
    max_order = max([b.order_index for b in position.bullets], default=-1)
    tags = [
        position.parent.company_name if position.parent else None,
        position.position_title,
        position.project_name,
    ]
    bullet = ExperienceBullet(position_id=position_id, content=content, tags=tags, order_index=max_order + 1)
    db.add(bullet)
    db.commit()
    db.refresh(bullet)
    return bullet


def update_bullet_content(db: Session, bullet_id: int, content: str) -> ExperienceBullet:
    bullet = db.get(ExperienceBullet, bullet_id)
    if bullet is None:
        raise ValueError(f"贡献句不存在：id={bullet_id}")
    content = (content or "").strip()
    if not content:
        raise ValueError("贡献句内容不能为空")
    if content != bullet.content:
        bullet.content = content
        # 内容变了，原来抽取的三元组（keywords/action_summary/result_summary）
        # 不再可信，清空后下次跑 backfill_bullet_triads 会自动重新抽取——见
        # 实施方案"关键设计决策记录"里"bullet 编辑联动"这条已经记下的延后决定，
        # 这里用"清空、交给 backfill 自然重算"这个最小改动实现，而不是在这里
        # 同步调用 LLM 重新抽取。
        bullet.keywords = None
        bullet.action_summary = None
        bullet.result_summary = None
    db.commit()
    db.refresh(bullet)
    return bullet


def delete_bullet(db: Session, bullet_id: int) -> bool:
    bullet = db.get(ExperienceBullet, bullet_id)
    if bullet is None:
        return False
    db.delete(bullet)
    db.commit()
    return True


# ---------- 教育经历 / 独立项目的手动增删改（打磨阶段后新增） ----------


def get_education_entries(db: Session) -> list[EducationEntry]:
    return db.query(EducationEntry).order_by(EducationEntry.order_index, EducationEntry.id).all()


def add_education_entry(
    db: Session,
    school: str,
    degree: str | None = None,
    location: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    is_current: bool = False,
) -> EducationEntry:
    school = (school or "").strip()
    if not school:
        raise ValueError("学校名不能为空")
    existing = db.query(EducationEntry).all()
    max_order = max([e.order_index for e in existing], default=-1)
    entry = EducationEntry(
        school=school,
        degree=(degree or "").strip() or None,
        location=(location or "").strip() or None,
        start_date=(start_date or "").strip() or None,
        end_date=(end_date or "").strip() or None,
        is_current=bool(is_current),
        order_index=max_order + 1,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def update_education_entry(db: Session, entry_id: int, fields_in: dict) -> EducationEntry:
    entry = db.get(EducationEntry, entry_id)
    if entry is None:
        raise ValueError(f"教育经历不存在：id={entry_id}")
    for key in ("school", "degree", "location", "start_date", "end_date"):
        if key in fields_in:
            value = (fields_in[key] or "").strip() or None
            setattr(entry, key, value)
    if "is_current" in fields_in:
        entry.is_current = bool(fields_in["is_current"])
    db.commit()
    db.refresh(entry)
    return entry


def delete_education_entry(db: Session, entry_id: int) -> bool:
    entry = db.get(EducationEntry, entry_id)
    if entry is None:
        return False
    db.delete(entry)
    db.commit()
    return True


def get_personal_projects(db: Session) -> list[PersonalProject]:
    return db.query(PersonalProject).order_by(PersonalProject.order_index, PersonalProject.id).all()


def add_personal_project(
    db: Session,
    project_name: str,
    start_date: str | None = None,
    end_date: str | None = None,
    is_current: bool = False,
) -> PersonalProject:
    project_name = (project_name or "").strip()
    if not project_name:
        raise ValueError("项目名称不能为空")
    existing = db.query(PersonalProject).all()
    max_order = max([p.order_index for p in existing], default=-1)
    project = PersonalProject(
        project_name=project_name,
        start_date=(start_date or "").strip() or None,
        end_date=(end_date or "").strip() or None,
        is_current=bool(is_current),
        order_index=max_order + 1,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def update_personal_project(db: Session, project_id: int, fields_in: dict) -> PersonalProject:
    project = db.get(PersonalProject, project_id)
    if project is None:
        raise ValueError(f"独立项目不存在：id={project_id}")
    for key in ("project_name", "start_date", "end_date"):
        if key in fields_in:
            value = (fields_in[key] or "").strip() or None
            setattr(project, key, value)
    if "is_current" in fields_in:
        project.is_current = bool(fields_in["is_current"])
    db.commit()
    db.refresh(project)
    return project


def delete_personal_project(db: Session, project_id: int) -> bool:
    project = db.get(PersonalProject, project_id)
    if project is None:
        return False
    db.delete(project)
    db.commit()
    return True


def add_personal_project_bullet(db: Session, project_id: int, content: str) -> PersonalProjectBullet:
    project = db.get(PersonalProject, project_id)
    if project is None:
        raise ValueError(f"独立项目不存在：id={project_id}")
    content = (content or "").strip()
    if not content:
        raise ValueError("贡献句内容不能为空")
    max_order = max([b.order_index for b in project.bullets], default=-1)
    bullet = PersonalProjectBullet(project_id=project_id, content=content, order_index=max_order + 1)
    db.add(bullet)
    db.commit()
    db.refresh(bullet)
    return bullet


def update_personal_project_bullet(db: Session, bullet_id: int, content: str) -> PersonalProjectBullet:
    bullet = db.get(PersonalProjectBullet, bullet_id)
    if bullet is None:
        raise ValueError(f"贡献句不存在：id={bullet_id}")
    content = (content or "").strip()
    if not content:
        raise ValueError("贡献句内容不能为空")
    bullet.content = content
    db.commit()
    db.refresh(bullet)
    return bullet


def delete_personal_project_bullet(db: Session, bullet_id: int) -> bool:
    bullet = db.get(PersonalProjectBullet, bullet_id)
    if bullet is None:
        return False
    db.delete(bullet)
    db.commit()
    return True
