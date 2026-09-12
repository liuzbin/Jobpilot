"""
Phase 4 自动化填表：题库（qa_bank）本地文本相似度检索。

设计选择（已和用户确认，见 docs/JobPilot_实施方案.md Phase 4 章节）：纯本地
字符 n-gram 特征哈希 + 余弦相似度，不调用任何模型 API，不需要新增模型配置
字段。选择字符 n-gram 而不是经典 TF-IDF，是因为 TF-IDF 的 IDF 项依赖于
整个语料库的统计量，qa_bank 会随着用户不断补充问答而持续变化，每次新增/
删除一条记录都要重新计算全库的 IDF、重新回填所有历史 embedding，成本和
复杂度都不必要；字符 n-gram 特征哈希是逐条独立计算的，不依赖语料库其他
文档，新增一条不影响历史记录，长期可维护性更好。选择字符 n-gram（而不是
分词后的词袋）还有一个好处：中英文混合文本不需要分词器（比如 jieba）也能
统一处理，同一套逻辑两种语言都适用。

哈希函数选择 `zlib.crc32` 而不是 Python 内置的 `hash()`：内置 `hash()`
对字符串是进程级随机化的（受 PYTHONHASHSEED 影响），同一个字符串在两次
进程启动之间会哈希出不同的值——如果拿它来算 embedding 并落库，那么"服务
重启前算好存库的向量"和"服务重启后重新计算的查询向量"对同一个字符串会
得到不同的哈希桶，向量就完全对不上了。`zlib.crc32` 是标准库里稳定、
跨进程确定性的哈希函数，天然满足"落库的 embedding 需要长期可比较"这个
要求。
"""

from __future__ import annotations

import zlib

import numpy as np

# 向量维度：不需要很大，题库量级预计是几十到几百条常见问题，512 维足够
# 把不同问题的 n-gram 分布区分开，同时保持每条 embedding 只有几 KB。
VECTOR_DIM = 512

# 同时使用 2-gram 和 3-gram：2-gram 对短问题（比如几个字的标题类问题）更
# 敏感，3-gram 能捕捉更长的局部语序信息，两者叠加比单一 n 值更稳健。
NGRAM_SIZES = (2, 3)


def _normalize_text(text: str) -> str:
    """归一化：去首尾空白、转小写、把连续空白折叠成单个空格。大小写和空白
    差异不应该影响相似度判断（"Are you willing..." 和 "are you willing..."
    应该被当成同一个问题）。"""
    return " ".join((text or "").strip().lower().split())


def _char_ngrams(text: str, n: int):
    if len(text) < n:
        if text:
            yield text
        return
    for i in range(len(text) - n + 1):
        yield text[i : i + n]


def compute_embedding(text: str) -> np.ndarray:
    """把一段文本编码成固定维度（VECTOR_DIM）、L2 归一化的向量。

    做法：对每个 n-gram（n 取 NGRAM_SIZES 里的值）算 crc32 哈希，对
    VECTOR_DIM 取模得到桶下标，往对应桶里累加 1（特征哈希 / hashing trick，
    经典用法参考 Vowpal Wabbit 等系统），最后做 L2 归一化，这样向量点积
    就等于余弦相似度，不需要额外再除以模长。

    空文本返回全零向量（范数为 0），调用方需要自行处理"没有可比较内容"的
    情况——全零向量和任何向量的点积都是 0，相似度天然最低，语义上也合理。
    """
    normalized = _normalize_text(text)
    vector = np.zeros(VECTOR_DIM, dtype=np.float64)
    if not normalized:
        return vector

    for n in NGRAM_SIZES:
        for gram in _char_ngrams(normalized, n):
            # 用 n 本身也参与哈希输入，避免"ab"(2-gram) 和某个恰好相同字符
            # 组合的 3-gram 哈希到完全相同的桶（虽然概率不高，但没必要留
            # 这个隐患）。
            bucket = zlib.crc32(f"{n}:{gram}".encode("utf-8")) % VECTOR_DIM
            vector[bucket] += 1.0

    norm = np.linalg.norm(vector)
    if norm > 0:
        vector = vector / norm
    return vector


def embedding_to_bytes(vector: np.ndarray) -> bytes:
    """落库格式：float32 小端连续字节，QABankEntry.embedding 是 LargeBinary。
    用 float32 而不是 float64 是因为特征哈希本来就是近似方法，float32 的
    精度绰绰有余，还能把每条记录的存储体积减半。"""
    return np.asarray(vector, dtype=np.float32).tobytes()


def bytes_to_embedding(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.float32).astype(np.float64)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """两个向量都已经 L2 归一化过的话，点积就是余弦相似度；这里为了稳妥
    起见（万一传进来的向量没有被归一化过）还是显式除一次模长，避免因为
    调用方传了未归一化的向量而算出 >1 或没有意义的相似度值。"""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))
