"""
用户画像的读写逻辑：基本信息表单 CRUD + 工作经历树（A/B/C 三层）的合并写入
与手动增删改。

关于"合并"的范围说明（Phase 1 曾经的一处有意简化，Phase 2 补完时按下面的
规则收窄了简化的范围，记入 docs/DEVELOPMENT_LOG.md）：
- 公司/项目按名称做不区分大小写的模糊匹配，匹配上了就复用，匹配不上就新增；
  这一条不涉及信息丢失，继续自动处理，不用打扰用户。
- 贡献句按去除首尾空白后的精确文本去重，已存在的句子不重复插入；这一条同样
  不用打扰用户。
- 贡献句如果不是精确重复、但和同一段职位下已有的某条贡献句高度相似（用
  difflib 算字符串相似度），说明很可能是同一件事的不同措辞，不能默默地
  当成两条不相关的贡献句自动都加进去（会导致画像里出现大量看起来重复的
  条目），也不能默默丢弃新的那条（可能新措辞更准确、更完整）——这种情况下
  交给用户选择：保留旧的/替换成新的/两条都要。
- 职位已经存在、但重新解析出的起止时间或"是否至今"和已有记录不一致时，
  同样不默默用新值覆盖旧值（有可能新解析反而不准），交给用户确认。
- 基本信息表单本身走"用户显式编辑就整体覆盖"的逻辑（Dashboard 表单提交视为
  用户已经确认过内容）；但简历上传自动抽取出的基本信息只用来"填补空白字段",
  不会覆盖用户已经手动填过的字段,避免解析误差覆盖掉用户确认过的准确信息。

除此之外，本文件也提供工作经历树的手动增删改：新增公司/职位/贡献句、编辑
职位字段、编辑贡献句内容、删除公司/职位/贡献句——Phase 1 只能靠重新上传
简历增量补充，画像里的错误没法直接改，这里补上直接编辑的入口。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.tables import ExperienceBullet, ExperienceEntry, ExperienceLevel, ProfileBasic

# 贡献句相似度阈值：高于这个值判定为"很可能是同一件事的不同措辞"，需要用户
# 确认怎么处理；低于这个值就当成两条不相关的贡献句直接都保留。取 0.82 是
# 观察典型改写（换几个词、调整语序）之后的经验值——既能抓住"基本是同一句话
# 微调措辞"的情况，也不会把两条主题相关但确实是不同事情的贡献句误判成冲突。
_BULLET_SIMILARITY_THRESHOLD = 0.82

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


def _position_label(position: ExperienceEntry) -> str:
    return " / ".join(filter(None, [position.position_title, position.project_name])) or "（未命名职位）"


def _check_position_field_conflicts(position: ExperienceEntry, position_in: dict) -> list[dict]:
    """职位已存在时，检查起止时间/是否至今是否和新解析出的值冲突。只有
    "已有值非空、新值也非空、且两者不同"才算冲突；已有值本来就是空的，
    直接当填空处理（不算冲突，也不用打扰用户）。"""
    conflicts: list[dict] = []
    company_name = position.parent.company_name if position.parent else None
    label = _position_label(position)

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


def merge_parsed_experience(db: Session, parsed: dict) -> MergeResult:
    """parsed 的结构见本文件顶部注释。做自动合并写入,返回统计结果，包括需要
    用户确认才会生效的冲突列表（result.bullet_conflicts / position_field_conflicts）。"""
    result = MergeResult()

    if parsed.get("basic"):
        result.basic_fields_filled = fill_blank_profile_basic_fields(db, parsed["basic"])

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

        existing_positions = {
            (_norm(p.position_title), _norm(p.project_name)): p for p in company.children
        }
        max_position_order = max([p.order_index for p in company.children], default=-1)

        for position_in in company_in.get("positions", []):
            position_title = (position_in.get("position_title") or "").strip()
            project_name = (position_in.get("project_name") or "").strip()
            key = (_norm(position_title), _norm(project_name))
            position = existing_positions.get(key)
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
                existing_positions[key] = position
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
    elif field_name not in ("start_date", "end_date"):
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
