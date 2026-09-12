"""
Phase 2 补完：题库表 `qa_bank` 的问答收集流程（实施方案 8/Phase 2 第 4 项：
"实现题库表 qa_bank 和最初建画像时的问答收集流程"）。

这里做"收集和复用清单"这一步：系统根据画像的目标职位生成一批常见的
投递自我介绍/主观题，用户在 Dashboard 上一次性作答，落进 qa_bank，供以后
投递时手动查阅复制。

Phase 4 补充：写入/更新一条记录时顺带算好 embedding 落库（见
app.services.qa_similarity），供自动化填表时做相似度检索——用户遇到一道
新的申请表主观题，先在题库里找语义最接近的历史问答，找到就直接复用/
提示用户确认，而不是每次都要重新手打。`find_similar_answer` 是这条检索
链路的入口。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.llm_client import LLMClient
from app.models.tables import ProfileBasic, QABankEntry, QASource
from app.services.qa_similarity import (
    bytes_to_embedding,
    compute_embedding,
    cosine_similarity,
    embedding_to_bytes,
)

# 低于这个相似度就认为"没有足够接近的历史问答"，不应该直接拿来自动填表
# （避免把风马牛不相及的答案填进一个新问题里），只作为"完全没有匹配"处理。
# 取 0.3 是经验阈值：字符 n-gram 哈希对完全不相关的两句话算出的相似度
# 通常明显低于这个数，而哪怕只是措辞不同的同一个问题也会明显高于它。
SIMILARITY_THRESHOLD = 0.3

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

    embedding_bytes = embedding_to_bytes(compute_embedding(question_text))

    if match is None:
        match = QABankEntry(
            question_text=question_text,
            answer_text=answer_text,
            source=source,
            jd_id=jd_id,
            embedding=embedding_bytes,
        )
        db.add(match)
    else:
        match.answer_text = answer_text
        match.embedding = embedding_bytes
        if jd_id is not None:
            match.jd_id = jd_id

    db.commit()
    db.refresh(match)
    return match


def backfill_qa_embeddings(db: Session) -> int:
    """给历史遗留的、embedding 为空的题库记录补算 embedding。

    出现这种记录的原因：Phase 2 阶段 `add_qa_entry` 还没有计算 embedding
    这个逻辑，那时候写入的记录 embedding 字段全是 NULL；Phase 4 上线后
    新写入的记录会自动带上 embedding，但历史数据需要跑一次这个函数补齐，
    不然相似度检索会漏掉这些旧记录。跟 profile_deepening 里
    `backfill_bullet_triads` 是同一种"新增一个字段/能力后，给历史数据补
    一次"的模式，命名也保持一致。"""
    entries = db.query(QABankEntry).filter(QABankEntry.embedding.is_(None)).all()
    for entry in entries:
        entry.embedding = embedding_to_bytes(compute_embedding(entry.question_text))
    if entries:
        db.commit()
    return len(entries)


def find_similar_answer(db: Session, question_text: str) -> tuple[QABankEntry | None, float]:
    """给一道新遇到的申请表问题，在题库里找相似度最高的历史问答。

    返回 (最相似的记录或 None, 相似度)。相似度低于 SIMILARITY_THRESHOLD
    时返回 (None, 相似度)——调用方应该视为"没有找到可用的历史答案"，而不是
    强行把一个不相关的答案塞进新问题里。"""
    question_text = (question_text or "").strip()
    if not question_text:
        return None, 0.0

    query_vector = compute_embedding(question_text)
    if not query_vector.any():
        return None, 0.0

    best_entry: QABankEntry | None = None
    best_score = 0.0
    for entry in db.query(QABankEntry).filter(QABankEntry.embedding.isnot(None)).all():
        candidate_vector = bytes_to_embedding(entry.embedding)
        score = cosine_similarity(query_vector, candidate_vector)
        if score > best_score:
            best_score = score
            best_entry = entry

    if best_entry is None or best_score < SIMILARITY_THRESHOLD:
        return None, best_score
    return best_entry, best_score


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
