"""
用户画像的读写逻辑：基本信息表单 CRUD + 工作经历树（A/B/C 三层）的合并写入。

关于"合并"的范围说明（Phase 1 的一处有意简化,记入 docs/DEVELOPMENT_LOG.md）：
原方案里设想的是"新旧内容有冲突时,弹出来让用户选择合并还是替换"这种交互式
diff。Phase 1 的目标是先跑通端到端打分闭环,所以这里先做成自动合并：
- 公司/项目按名称做不区分大小写的模糊匹配,匹配上了就复用,匹配不上就新增；
- 贡献句按去除首尾空白后的精确文本去重,已存在的句子不重复插入；
- 基本信息表单本身走"用户显式编辑就整体覆盖"的逻辑（Dashboard 表单提交视为
  用户已经确认过内容）；但简历上传自动抽取出的基本信息只用来"填补空白字段",
  不会覆盖用户已经手动填过的字段,避免解析误差覆盖掉用户确认过的准确信息。
交互式的手动合并/替换选择留到 Phase 2 再做。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.tables import ExperienceBullet, ExperienceEntry, ExperienceLevel, ProfileBasic

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


def merge_parsed_experience(db: Session, parsed: dict) -> MergeResult:
    """parsed 的结构见本文件顶部注释。做自动合并写入,返回统计结果。"""
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
