"""简历文件解析：提取纯文本 + 调用 LLM 做结构化抽取。"""

from __future__ import annotations

from pathlib import Path

from app.core.llm_client import LLMClient

RESUME_EXTRACTION_SYSTEM_PROMPT = """\
你是一个简历信息抽取助手。你会收到一段简历原文纯文本，需要把其中的工作经历/项目经历
按照三层结构抽取出来：A 层是公司，B 层是该公司下的项目+岗位+时间区间，C 层是这段经历
里的具体贡献，按句子切分。同时如果简历里包含姓名、邮箱、电话、LinkedIn、GitHub、学校、
学历、当前所在地这些基本信息，也一并抽取。

严格按下面的 JSON 结构输出，不要输出任何多余的解释文字：
{
  "basic": {
    "full_name": "姓名或 null",
    "email": "邮箱或 null",
    "phone": "电话或 null",
    "linkedin_url": "领英链接或 null",
    "github_url": "GitHub 链接或 null",
    "school": "学校或 null",
    "education": "最高学历或 null",
    "current_location": "当前所在地或 null"
  },
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
  ]
}

只抽取简历里真实写出来的内容，不要编造、不要推测简历里没有的信息。抽取不到的字段填 null。
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
    result.setdefault("companies", [])
    return result
