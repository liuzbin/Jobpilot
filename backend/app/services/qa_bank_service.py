"""
Phase 2 补完：题库表 `qa_bank` 的问答收集流程（实施方案 8/Phase 2 第 4 项：
"实现题库表 qa_bank 和最初建画像时的问答收集流程"）。

这里只做"收集和复用清单"这一步：系统根据画像的目标职位生成一批常见的
投递自我介绍/主观题，用户在 Dashboard 上一次性作答，落进 qa_bank，供以后
投递时手动查阅复制。embedding 字段暂时留空——按实施方案技术栈总览，
"numpy（相似度计算）"明确标注"待 Phase 4 引入"，真正基于向量相似度的
自动检索/填充是 Phase 4 自动化填表的范围，这里不提前实现，避免在还用不上
的地方引入额外依赖和复杂度。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.llm_client import LLMClient
from app.models.tables import ProfileBasic, QABankEntry, QASource

COMMON_QA_SYSTEM_PROMPT = """\
你负责为候选人生成一批常见的求职投递自我介绍/主观题问题（不是技术面试题），
这些问题几乎每次投递都会反复遇到，候选人可以提前想清楚、写好答案，以后
投递时直接复用或者稍作修改，比如"请简单介绍一下你自己"、"你为什么想加入
这个行业/岗位"、"你最大的优势/劣势是什么"这一类。

结合候选人的目标职位，生成 5-8 个这样的问题，避免和具体某个 JD 强绑定
（这些问题应该是跨投递通用的）。

严格按下面的 JSON 结构输出：
{"questions": ["...", "..."]}
"""


def generate_common_qa_questions(profile: ProfileBasic, light_client: LLMClient) -> list[str]:
    """生成一批通用投递问答问题，过滤掉模型返回里非字符串/空白的脏数据。"""
    target = profile.target_title or "（未填写目标职位）"
    user_prompt = f"候选人目标职位：{target}"
    result = light_client.complete_json(COMMON_QA_SYSTEM_PROMPT, user_prompt)
    raw_questions = result.get("questions") or []
    return [q.strip() for q in raw_questions if isinstance(q, str) and q.strip()]


def _norm_question(text: str) -> str:
    return (text or "").strip().lower()


def add_qa_entry(
    db: Session,
    question_text: str,
    answer_text: str,
    source: QASource = QASource.ONBOARDING,
    jd_id: int | None = None,
) -> QABankEntry | None:
    """新增或更新一条题库记录：按问题文本（去除首尾空白、不区分大小写）去重，
    已存在就更新答案，不重复插入。答案为空白时视为用户跳过，不写入/不覆盖。"""
    question_text = (question_text or "").strip()
    answer_text = (answer_text or "").strip()
    if not question_text or not answer_text:
        return None

    existing = (
        db.query(QABankEntry)
        .filter(QABankEntry.question_text.isnot(None))
        .all()
    )
    match = next((e for e in existing if _norm_question(e.question_text) == _norm_question(question_text)), None)

    if match is None:
        match = QABankEntry(
            question_text=question_text,
            answer_text=answer_text,
            source=source,
            jd_id=jd_id,
        )
        db.add(match)
    else:
        match.answer_text = answer_text
        if jd_id is not None:
            match.jd_id = jd_id

    db.commit()
    db.refresh(match)
    return match


def save_qa_answers(
    db: Session,
    qa_pairs: list[dict],
    source: QASource = QASource.ONBOARDING,
    jd_id: int | None = None,
) -> int:
    """批量保存一轮问答，跳过空白答案，返回实际保存/更新的条数。"""
    saved = 0
    for pair in qa_pairs:
        entry = add_qa_entry(
            db,
            question_text=pair.get("question", ""),
            answer_text=pair.get("answer", ""),
            source=source,
            jd_id=jd_id,
        )
        if entry is not None:
            saved += 1
    return saved


def list_qa_entries(db: Session) -> list[QABankEntry]:
    # 按 id 倒序而不是 created_at 倒序：SQLite 的 CURRENT_TIMESTAMP 只有秒级
    # 精度，同一秒内连续插入的记录 created_at 会相同，用自增 id 才能可靠地
    # 反映真实的插入顺序。
    return db.query(QABankEntry).order_by(QABankEntry.id.desc()).all()


def delete_qa_entry(db: Session, entry_id: int) -> bool:
    entry = db.get(QABankEntry, entry_id)
    if entry is None:
        return False
    db.delete(entry)
    db.commit()
    return True
