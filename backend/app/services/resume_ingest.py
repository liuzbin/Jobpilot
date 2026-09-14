"""简历文件解析：提取纯文本 + 结构化抽取。

打磨阶段用户反馈第 2 点：简历按 JobPilot 的 MD 模板格式上传时，不需要 LLM
就能确定性地解析（见 `resume_md_parser.py`），只有规则解析识别不出来的
情况（本来就不是 md，或者是随手写的自由格式 md）才回退到这里的 LLM 抽取——
`extract_and_structure` 是这条"规则优先、LLM 兜底"路径的唯一入口，调用方
（routes_dashboard.py）不需要关心到底是哪条路径产出的结果，两条路径的返回
结构是同构的。"""

from __future__ import annotations

from pathlib import Path

from app.core.llm_client import LLMClient
from app.services.resume_md_parser import parse_resume_markdown

RESUME_EXTRACTION_SYSTEM_PROMPT = """\
你是一个简历信息抽取助手。你会收到一段简历原文纯文本，需要把其中的工作经历/项目经历
按照三层结构抽取出来：A 层是公司，B 层是该公司下的项目+岗位+时间区间，C 层是这段经历
里的具体贡献，按句子切分。同时如果简历里包含姓名、邮箱、电话、LinkedIn、GitHub、学校、
学历、当前所在地这些基本信息，个人总结/技能这两段背景介绍，教育经历（可能不止一条），
不挂靠任何公司的独立项目经历，也都一并抽取。

严格按下面的 JSON 结构输出，不要输出任何多余的解释文字：
{
  "basic": {
    "full_name": "姓名或 null",
    "email": "邮箱或 null",
    "phone": "电话或 null",
    "linkedin_url": "领英链接或 null",
    "github_url": "GitHub 链接或 null",
    "school": "最高学历对应的学校或 null",
    "education": "最高学历或 null",
    "current_location": "当前所在地或 null",
    "resume_summary": "个人总结，多条就用换行符分隔成多行，没有这部分就填 null",
    "skills_text": "技能列表，多条就用换行符分隔成多行（每行可以是"分类: 技能1, 技能2"这种格式），没有就填 null"
  },
  "education_entries": [
    {
      "school": "学校",
      "degree": "学位/专业",
      "location": "所在地或 null",
      "start_date": "YYYY-MM 或 YYYY",
      "end_date": "YYYY-MM 或 YYYY 或 null",
      "is_current": true或false
    }
  ],
  "companies": [
    {
      "company_name": "公司名",
      "positions": [
        {
          "position_title": "岗位名称",
          "project_name": "项目名称（没有就用岗位名称重复一次）",
          "start_date": "YYYY-MM 格式，不确定月份就只填 YYYY",
          "end_date": "YYYY-MM 或 null（如果是至今）",
          "is_current": true或false,
          "bullets": ["具体贡献句子1", "具体贡献句子2"]
        }
      ]
    }
  ],
  "projects": [
    {
      "project_name": "不挂靠任何公司的独立/课外项目名称",
      "start_date": "YYYY-MM 或 YYYY",
      "end_date": "YYYY-MM 或 YYYY 或 null",
      "is_current": true或false,
      "bullets": ["具体贡献句子1", "具体贡献句子2"]
    }
  ]
}

只抽取简历里真实写出来的内容，不要编造、不要推测简历里没有的信息。抽取不到的字段填 null
或空数组。工作经历（挂靠公司）和独立项目（不挂靠公司）要分清楚，不要把独立项目错误地
塞进 companies 里，也不要把工作经历错误地塞进 projects 里。
"""


def extract_text(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix in (".txt", ".md"):
        return file_path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".pdf":
        import pdfplumber

        chunks = []
        with pdfplumber.open(str(file_path)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                chunks.append(text)
        return "\n".join(chunks)
    if suffix == ".docx":
        import docx

        document = docx.Document(str(file_path))
        return "\n".join(p.text for p in document.paragraphs)
    raise ValueError(f"不支持的简历文件格式: {suffix}（目前支持 .pdf / .docx / .txt / .md）")


def structure_resume_text(raw_text: str, llm_client: LLMClient) -> dict:
    if not raw_text.strip():
        raise ValueError("简历内容为空，无法抽取")
    result = llm_client.complete_json(RESUME_EXTRACTION_SYSTEM_PROMPT, raw_text)
    result.setdefault("basic", {})
    result.setdefault("education_entries", [])
    result.setdefault("companies", [])
    result.setdefault("projects", [])
    return result


def extract_and_structure(file_path: Path, raw_text: str, llm_client: LLMClient) -> dict:
    """规则解析优先、LLM 兜底的唯一入口：`.md` 文件先尝试按 JobPilot 模板
    约定做确定性解析（不消耗任何 LLM 调用），解析不出来（不是 md，或者是
    没有任何一个识别得出的章节标题的自由格式 md）才回退到 LLM 抽取。

    `raw_text` 由调用方通过 `extract_text` 提前拿到——两条路径都要用到它，
    避免重复读文件；`file_path` 只用来判断扩展名。"""
    if file_path.suffix.lower() == ".md":
        parsed = parse_resume_markdown(raw_text)
        if parsed is not None:
            return parsed
    return structure_resume_text(raw_text, llm_client)
