"""
Phase 4 自动化填表：核心决策逻辑（"给插件扫描到的一批表单字段，算出每个
字段该填什么"），完全不涉及浏览器 DOM——DOM 层面的字段扫描/选择器规则
在插件侧（extension/content_scripts/），这里只处理"字段标签文本 → 该填的
值"这一步纯逻辑，方便脱离 Chrome 用 pytest 覆盖。

安全设计（本项目一贯的"绝不允许 AI 静默替用户做出有后果的决定"原则在这里
的具体体现）：任何涉及合规/法律声明性质的字段——工作授权、签证担保、
EEO/人口统计自报（性别、种族、退伍军人身份、残障状态）、犯罪记录/背景
调查等——只要控件类型是 select/radio/checkbox（也就是"替用户做一次离散的
法律选择"），就一律不自动填，不管标签映射结果是什么；这里做了两层防御：
1) COMPLIANCE_SENSITIVE_FIELD_KEYS 白名单机制：SAFE_CHOICE_FIELD_KEYS 之外
   的字段一律不允许通过 select/radio/checkbox 自动选择；
2) 独立于字段映射结果的标签关键词兜底扫描（_looks_like_compliance_label）：
   哪怕模型把标签错误地映射成了某个"安全"字段，只要标签文本本身命中了
   工作授权/签证/EEO/背景调查这类关键词、且控件是选择类，也强制跳过。
这两层任何一层触发都会跳过，两层同时失效的概率远低于单层。
纯文本/多行文本框（textarea/text/email 等）里"如实转述用户自己画像里
填写的 work_authorization"不受这个限制——那只是重复用户自己已经陈述过的
事实，不是替用户做一次新的法律选择。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.llm_client import LLMClient
from app.models.tables import ProfileBasic
from app.services.profile_service import get_or_create_profile_basic
from app.services.qa_bank_service import find_similar_answer

# 认为是"选择类"控件的 input_type 取值（由插件侧扫描时统一归一化后传入）。
CHOICE_INPUT_TYPES = {"select", "radio", "checkbox"}
# 认为是"自由文本"控件的 input_type 取值,essay/qa_bank 匹配、以及
# work_authorization 的文字转述都只作用于这一类控件。
TEXT_INPUT_TYPES = {"text", "textarea", "email", "tel", "url", "number"}

# 画像字段规格：field_key -> 用于关键词初筛的候选标签片段（小写、已经去除
# 空白，命中任意一个就算匹配到这个 field_key）。放在最前面按关键词直接
# 命中，是为了绝大多数常见字段（姓名/邮箱/电话/领英链接……）不需要浪费一次
# LLM 调用就能填上，只有关键词模式覆盖不到的标签才会进入批量 LLM 映射。
PROFILE_FIELD_SPECS: dict[str, list[str]] = {
    "full_name": ["full name", "your name", "姓名", "full legal name"],
    "first_name": ["first name", "given name"],
    "last_name": ["last name", "family name", "surname"],
    "email": ["email"],
    "phone": ["phone", "mobile", "手机号", "电话"],
    "linkedin_url": ["linkedin"],
    "github_url": ["github"],
    "school": ["school", "university", "college", "毕业院校", "学校"],
    "education": ["education", "degree", "学历"],
    "years_experience": ["years of experience", "years of relevant experience", "工作年限"],
    "current_location": ["current location", "current city", "所在城市", "现居地"],
    "target_location": [
        "preferred location",
        "desired location",
        "location you're applying",
        "期望工作地",
    ],
    "work_authorization": [
        "work authorization",
        "authorized to work",
        "legally authorized",
        "工作授权",
    ],
    "target_title": ["desired title", "position applying for", "期望职位"],
}

# 只有这些 field_key 允许通过选择类控件（select/radio/checkbox）自动选择。
# 注意 work_authorization 特意不在这个白名单里——即使模型把某个选择类
# 控件标签映射成了 work_authorization,也必须跳过,只有自由文本控件才能
# 转述这个字段。
SAFE_CHOICE_FIELD_KEYS = {"current_location", "target_location"}

# 标签关键词兜底扫描：任意命中即认为是合规/法律声明性质的字段，如果控件
# 又是选择类，无论字段映射是什么都强制跳过。这一层独立于 LLM/关键词映射
# 结果，专门防止映射出错导致误填。
COMPLIANCE_LABEL_KEYWORDS = [
    "work authorization",
    "authorized to work",
    "require sponsorship",
    "visa sponsorship",
    "need sponsorship",
    "security clearance",
    "criminal",
    "felony",
    "background check",
    "gender",
    "race",
    "ethnicity",
    "veteran",
    "disability",
    "sexual orientation",
    "eeo",
    "工作授权",
    "签证",
    "背景调查",
    "犯罪记录",
    "性别",
    "种族",
    "退伍军人",
    "残障",
]

LABEL_MAPPING_SYSTEM_PROMPT = """\
你负责把求职申请表单里的字段标签（label）映射到候选人画像里的字段 key，
或者判断这是一道需要候选人主观作答的问答题（比如"为什么想加入我们"、
"请描述一个你解决过的难题"这种）。

可选的画像字段 key 有：
{field_keys}

规则：
1. 如果标签明显对应上面某个字段 key,输出该 key。
2. 如果标签是一道需要候选人主观组织语言回答的问题（不是画像里的固定信息）,
   把 is_essay_question 设为 true,field_key 设为 null。
3. 如果既不匹配任何字段 key、也不是主观问答题（比如你完全看不懂这是什么,
   或者是文件上传类字段的说明文字）,field_key 和 is_essay_question 都设为
   null/false。
4. 不要自己发明字段 key,只能从上面给的列表里选。

严格按下面的 JSON 结构输出，index 要和输入一一对应：
{{"mappings": [{{"index": 0, "field_key": "email", "is_essay_question": false}}, ...]}}
"""


@dataclass
class ScannedField:
    field_id: str
    label: str
    input_type: str
    options: list[dict] = field(default_factory=list)


@dataclass
class AutofillAction:
    field_id: str
    action: str  # "fill" | "select" | "skip"
    value: str | None = None
    source: str | None = None  # "profile" | "qa_bank" | None
    reason: str | None = None
    similarity: float | None = None


def _looks_like_compliance_label(label: str) -> bool:
    normalized = (label or "").strip().lower()
    return any(keyword in normalized for keyword in COMPLIANCE_LABEL_KEYWORDS)


def _match_by_keyword(label: str) -> str | None:
    normalized = (label or "").strip().lower()
    if not normalized:
        return None
    for field_key, aliases in PROFILE_FIELD_SPECS.items():
        if any(alias in normalized for alias in aliases):
            return field_key
    return None


def _profile_value(profile: ProfileBasic, field_key: str) -> str | None:
    if field_key == "first_name":
        parts = (profile.full_name or "").strip().split()
        return parts[0] if parts else None
    if field_key == "last_name":
        parts = (profile.full_name or "").strip().split()
        return parts[-1] if len(parts) > 1 else None
    if field_key == "years_experience":
        return None if profile.years_experience is None else str(profile.years_experience)
    return getattr(profile, field_key, None)


def _map_labels_with_llm(labels: list[str], light_client: LLMClient) -> dict[int, dict]:
    """批量调用一次 LLM,把多个未能靠关键词匹配上的标签一起丢进一次请求里
    （沿用 Phase 2 起就确立的"批量调用而不是逐条调用"惯例），减少调用次数
    和延迟。返回 index -> {"field_key": str|None, "is_essay_question": bool}。"""
    if not labels:
        return {}
    field_keys_desc = "\n".join(f"- {key}" for key in PROFILE_FIELD_SPECS)
    system_prompt = LABEL_MAPPING_SYSTEM_PROMPT.format(field_keys=field_keys_desc)
    user_prompt = "\n".join(f"{i}: {label}" for i, label in enumerate(labels))
    result = light_client.complete_json(system_prompt, user_prompt)
    mappings = result.get("mappings") or []
    by_index: dict[int, dict] = {}
    for item in mappings:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if not isinstance(idx, int):
            continue
        field_key = item.get("field_key")
        if field_key is not None and field_key not in PROFILE_FIELD_SPECS:
            field_key = None
        by_index[idx] = {
            "field_key": field_key,
            "is_essay_question": bool(item.get("is_essay_question")),
        }
    return by_index


def build_autofill_plan(
    db: Session,
    fields: list[dict],
    light_client: LLMClient | None,
    jd_id: int | None = None,
) -> list[AutofillAction]:
    """给插件扫描到的一批表单字段生成填表计划。

    fields 每一项形如
    {"field_id": str, "label": str, "input_type": str, "options": [...]|None}
    （options 只在 input_type 是选择类控件时有意义）。

    light_client 允许为 None（模型还没配置的情况）：这时关键词能匹配上的
    字段照常处理,关键词匹配不上、需要 LLM 兜底判断的字段全部标记为
    unmapped,不会因为模型没配置就整个功能不可用。
    """
    profile = get_or_create_profile_basic(db)
    scanned = [
        ScannedField(
            field_id=str(f.get("field_id")),
            label=(f.get("label") or "").strip(),
            input_type=(f.get("input_type") or "text").strip().lower(),
            options=list(f.get("options") or []),
        )
        for f in fields
    ]

    # 第一遍：关键词直接命中的字段先算出来，收集剩下需要 LLM 兜底的字段。
    # unresolved_index_by_field_id 记录每个未命中字段在"喂给 LLM 的标签
    # 列表"里的下标——不能用 list.index(sf) 反查,因为 ScannedField 是按
    # 字段值比较相等的 dataclass,两个标签、类型、options 都相同但
    # field_id 不同的字段会被 index() 错误地识别成同一个,必须显式建一份
    # field_id -> index 的映射。
    keyword_matches: dict[str, str] = {}
    unresolved: list[ScannedField] = []
    unresolved_index_by_field_id: dict[str, int] = {}
    for sf in scanned:
        field_key = _match_by_keyword(sf.label)
        if field_key is not None:
            keyword_matches[sf.field_id] = field_key
        else:
            unresolved_index_by_field_id[sf.field_id] = len(unresolved)
            unresolved.append(sf)

    llm_mappings: dict[int, dict] = {}
    if unresolved and light_client is not None:
        try:
            llm_mappings = _map_labels_with_llm([sf.label for sf in unresolved], light_client)
        except Exception:  # noqa: BLE001
            # LLM 兜底失败（网络错误/解析失败等）不应该让整个填表计划报错,
            # 退化成"这些字段全部 unmapped",用户仍然可以手动填。
            llm_mappings = {}

    actions: list[AutofillAction] = []
    for sf in scanned:
        actions.append(
            _build_action_for_field(
                sf, keyword_matches, unresolved_index_by_field_id, llm_mappings, profile, db, jd_id
            )
        )
    return actions


def _build_action_for_field(
    sf: ScannedField,
    keyword_matches: dict[str, str],
    unresolved_index_by_field_id: dict[str, int],
    llm_mappings: dict[int, dict],
    profile: ProfileBasic,
    db: Session,
    jd_id: int | None,
) -> AutofillAction:
    is_choice = sf.input_type in CHOICE_INPUT_TYPES

    # 独立于字段映射结果的第二道防线：标签本身像合规/法律声明字段,且是
    # 选择类控件,直接跳过,不再往下走字段映射逻辑。
    if is_choice and _looks_like_compliance_label(sf.label):
        return AutofillAction(
            field_id=sf.field_id,
            action="skip",
            reason="compliance_sensitive_choice_control",
        )

    field_key = keyword_matches.get(sf.field_id)
    is_essay_question = False
    if field_key is None and sf.field_id in unresolved_index_by_field_id:
        mapping = llm_mappings.get(unresolved_index_by_field_id[sf.field_id])
        if mapping is not None:
            field_key = mapping.get("field_key")
            is_essay_question = mapping.get("is_essay_question", False)

    if field_key is not None:
        if is_choice and field_key not in SAFE_CHOICE_FIELD_KEYS:
            # 白名单机制：即使映射出了字段 key,选择类控件也只允许安全字段
            # 自动选择,其余一律跳过（例如 work_authorization 映射到了一个
            # select 控件——只有自由文本才允许转述这个字段）。
            return AutofillAction(
                field_id=sf.field_id,
                action="skip",
                reason="field_not_allowed_for_choice_control",
            )
        value = _profile_value(profile, field_key)
        if not value:
            return AutofillAction(field_id=sf.field_id, action="skip", reason="profile_field_empty")
        if is_choice:
            return AutofillAction(field_id=sf.field_id, action="select", value=value, source="profile")
        return AutofillAction(field_id=sf.field_id, action="fill", value=value, source="profile")

    if is_essay_question and sf.input_type in TEXT_INPUT_TYPES:
        match, score = find_similar_answer(db, sf.label)
        if match is not None:
            return AutofillAction(
                field_id=sf.field_id,
                action="fill",
                value=match.answer_text,
                source="qa_bank",
                similarity=score,
            )
        return AutofillAction(field_id=sf.field_id, action="skip", reason="no_similar_qa_bank_entry")

    return AutofillAction(field_id=sf.field_id, action="skip", reason="unmapped")
