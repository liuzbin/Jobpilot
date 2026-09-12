"""Phase 4：题库本地文本相似度（app/services/qa_similarity.py）。"""

from __future__ import annotations

import numpy as np

from app.services.qa_similarity import (
    VECTOR_DIM,
    bytes_to_embedding,
    compute_embedding,
    cosine_similarity,
    embedding_to_bytes,
)


def test_compute_embedding_returns_fixed_dimension_unit_vector():
    vector = compute_embedding("请介绍一下你自己")
    assert vector.shape == (VECTOR_DIM,)
    assert abs(np.linalg.norm(vector) - 1.0) < 1e-9


def test_compute_embedding_blank_text_returns_zero_vector():
    vector = compute_embedding("   ")
    assert np.linalg.norm(vector) == 0.0


def test_compute_embedding_is_deterministic_across_calls():
    # 这是整个方案能落库复用的前提：同一段文本无论算多少次、无论进程有没有
    # 重启过，都必须得到完全一样的向量,不能依赖 Python 内置 hash() 那种
    # 进程级随机化的哈希（这正是选用 zlib.crc32 的原因）。
    a = compute_embedding("你为什么想加入这个行业")
    b = compute_embedding("你为什么想加入这个行业")
    assert np.array_equal(a, b)


def test_compute_embedding_ignores_case_and_surrounding_whitespace():
    a = compute_embedding("  Are you willing to relocate?  ")
    b = compute_embedding("are you willing to relocate?")
    assert np.array_equal(a, b)


def test_similar_questions_score_higher_than_unrelated_ones():
    base = compute_embedding("请简单介绍一下你自己")
    paraphrase = compute_embedding("请简单地自我介绍一下")
    unrelated = compute_embedding("你期望的薪资范围是多少")

    score_paraphrase = cosine_similarity(base, paraphrase)
    score_unrelated = cosine_similarity(base, unrelated)

    assert score_paraphrase > score_unrelated


def test_cosine_similarity_identical_vector_is_one():
    vector = compute_embedding("你最大的优势是什么")
    assert abs(cosine_similarity(vector, vector) - 1.0) < 1e-9


def test_cosine_similarity_zero_vector_is_zero():
    zero = np.zeros(VECTOR_DIM)
    other = compute_embedding("任意文本")
    assert cosine_similarity(zero, other) == 0.0


def test_embedding_bytes_roundtrip_preserves_values_within_float32_precision():
    vector = compute_embedding("你的职业规划是什么")
    restored = bytes_to_embedding(embedding_to_bytes(vector))
    assert restored.shape == vector.shape
    assert np.allclose(vector, restored, atol=1e-6)
