"""
Phase 2："画像深化"——见实施方案 4/5.1。两件事：

1. bullet 三元组抽取：把每条已有 `content`（原文，事实来源，永远不改）拆解成
   "关键词 + 行为 + 结果" 三个衍生字段，写回 `ExperienceBullet.keywords` /
   `action_summary` / `result_summary`，供简历重制阶段做关键词匹配和内容重组。
2. 追问式访谈：不是让用户对着空文本框写项目背景作文，而是系统先看这个项目
   已有的信息够不够，针对性地生成几个问题，用户逐条回答（可跳过）之后，系统
   把问答整理成一段连贯文本写回 `ExperienceEntry.background_notes`，原始问答
   保留在 `background_qa` 里方便复查和追加。

两件事都是"轻量模型做抽取/整理"的活，不涉及价值判断，走轻量槽位。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.llm_client import LLMClient
from app.models.tables import ExperienceBullet, ExperienceEntry, ExperienceLevel

# ---------- bullet 三元组抽取 ----------

BULLET_TRIAD_SYSTEM_PROMPT = """\
你是一个简历内容结构化助手。你会收到同一段工作/项目经历下的若干条贡献句（bullet）
原文，需要把每一条都拆解成"关键词 + 行为 + 结果"三部分，模拟 HR/ATS 筛简历时
实际抓取信息的方式（他们抓的是关键词、做了什么、什么结果，不是逐句做阅读理解）。

严格按下面的 JSON 结构输出，不要输出任何多余的解释文字：
{
  "triads": [
    {
      "keywords": ["这句话里出现的具体技术/领域关键词，逐条列出，没有就是空数组"],
      "action_summary": "这句话描述的核心行为，短语级别，不要照抄整句原文",
      "result_summary": "这句话描述的结果/成效，没有明确结果就填 null"
    }
  ]
}

triads 数组的长度和顺序必须和输入的 bullet 列表严格一一对应。只做拆解和提炼，
不要编造原文里没有的关键词或结果。
"""


def extract_bullet_triads(bullet_contents: list[str], llm_client: LLMClient) -> list[dict]:
    """对一组同属一个 position 的 bullet 原文做批量三元组抽取，返回和输入等长、
    顺序一致的 triad 列表。批量而不是逐条调用，是为了让模型看到同一段经历下的
    全部 bullet，抽取出来的关键词风格更一致，也省调用次数。
    """
    if not bullet_contents:
        return []
    numbered = "\n".join(f"{i + 1}. {text}" for i, text in enumerate(bullet_contents))
    result = llm_client.complete_json(BULLET_TRIAD_SYSTEM_PROMPT, numbered)
    triads = result.get("triads") or []
    # 防御性处理：模型返回数量对不上时，按输入长度截断/补空，不让上层因为
    # 数组越界而崩溃——三元组本来就是辅助索引，抽取失败不应该影响原始数据。
    normalized: list[dict] = []
    for i in range(len(bullet_contents)):
        if i < len(triads) and isinstance(triads[i], dict):
            item = triads[i]
        else:
            item = {}
        normalized.append(
            {
                "keywords": item.get("keywords") or [],
                "action_summary": item.get("action_summary") or None,
                "result_summary": item.get("result_summary") or None,
            }
        )
    return normalized


def backfill_bullet_triads(db: Session, llm_client: LLMClient) -> int:
    """给所有还没有三元组衍生字段的 bullet（`keywords is None`）补跑一遍抽取，
    按 position 分组批量调用。返回本次更新的 bullet 数量。简历合并入库
    （`merge_parsed_experience`）之后可以紧接着调用这个函数；已经手动深化过、
    有三元组的 bullet 不会被重复处理。
    """
    positions = (
        db.query(ExperienceEntry).filter(ExperienceEntry.level == ExperienceLevel.POSITION).all()
    )
    updated = 0
    for position in positions:
        pending: list[ExperienceBullet] = [b for b in position.bullets if b.keywords is None]
        if not pending:
            continue
        triads = extract_bullet_triads([b.content for b in pending], llm_client)
        for bullet, triad in zip(pending, triads):
            bullet.keywords = triad["keywords"]
            bullet.action_summary = triad["action_summary"]
            bullet.result_summary = triad["result_summary"]
            updated += 1
    if updated:
        db.commit()
    return updated


# ---------- 追问式访谈 ----------

BACKGROUND_QUESTIONS_SYSTEM_PROMPT = """\
你是一个帮用户完善简历项目背景的助手。你会收到某一段工作/项目经历的岗位名、
项目名、已有的贡献句列表，以及（如果有）已经记录过的背景问答。你的任务是判断
"项目背景与理解"这块信息目前是否足够支撑后续"判断某个技能是否和这个项目的
技术背景相关"这类推理，如果不够，提出 3-5 个有针对性的问题，帮用户把背景信息
补完整——例如项目的技术架构、数据规模/业务量级、用户具体负责的模块或环节、
遇到的技术挑战或权衡。如果已有信息已经比较完整，可以只提 1-2 个补充性问题，
甚至返回空列表。不要问已经在贡献句或已有问答里明确回答过的问题。

严格按下面的 JSON 结构输出：
{
  "questions": ["问题1", "问题2", ...]
}
"""

BACKGROUND_SYNTHESIS_SYSTEM_PROMPT = """\
你是一个帮用户整理项目背景描述的助手。你会收到某段工作/项目经历的岗位名、
项目名、已有的贡献句列表，以及用户对一系列背景问题的问答记录（可能包含多轮）。
请把这些问答整理成一段连贯、自然的中文项目背景描述,涵盖项目的技术架构、
数据规模/业务量级、用户具体负责的部分、以及技术挑战等能反映出的信息，不要
逐条罗列问答，也不要添加用户没有提到过的内容。

严格按下面的 JSON 结构输出：
{
  "background_notes": "整理后的项目背景描述文本"
}
"""


def _position_context(position: ExperienceEntry) -> str:
    bullets_text = "\n".join(f"- {b.content}" for b in position.bullets) or "（暂无贡献句）"
    qa_text = "\n".join(
        f"Q: {qa.get('question', '')}\nA: {qa.get('answer', '')}"
        for qa in (position.background_qa or [])
    ) or "（暂无历史问答）"
    return (
        f"岗位/项目：{position.position_title or ''} / {position.project_name or ''}\n"
        f"已有贡献句：\n{bullets_text}\n\n"
        f"已有背景描述：{position.background_notes or '（暂无）'}\n\n"
        f"历史问答：\n{qa_text}"
    )


def generate_background_questions(position: ExperienceEntry, llm_client: LLMClient) -> list[str]:
    """给某个 B 层条目生成一轮追问式访谈的问题列表。由用户在项目详情页手动
    点击"深化这段经历"触发，不在简历上传后自动弹出。"""
    result = llm_client.complete_json(BACKGROUND_QUESTIONS_SYSTEM_PROMPT, _position_context(position))
    questions = result.get("questions") or []
    return [q for q in questions if isinstance(q, str) and q.strip()]


def merge_background_answers(
    db: Session,
    experience_entry_id: int,
    qa_pairs: list[dict],
    llm_client: LLMClient,
) -> ExperienceEntry:
    """把用户这一轮的问答追加进 background_qa，再用 LLM 把累计问答整理成一段
    连贯文本写回 background_notes。qa_pairs 形如 [{"question": ..., "answer": ...}]，
    答案为空/纯空白的问题会被跳过（用户选择跳过这个问题）。
    """
    position = db.get(ExperienceEntry, experience_entry_id)
    if position is None or position.level != ExperienceLevel.POSITION:
        raise ValueError(f"experience_entry_id={experience_entry_id} 不是一个有效的项目/岗位条目")

    answered = [
        {"question": qa.get("question", ""), "answer": qa.get("answer", "").strip()}
        for qa in qa_pairs
        if (qa.get("answer") or "").strip()
    ]
    if not answered:
        return position

    position.background_qa = [*(position.background_qa or []), *answered]
    result = llm_client.complete_json(BACKGROUND_SYNTHESIS_SYSTEM_PROMPT, _position_context(position))
    position.background_notes = result.get("background_notes") or position.background_notes
    db.commit()
    db.refresh(position)
    return position
