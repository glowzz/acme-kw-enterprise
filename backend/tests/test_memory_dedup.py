"""判重纯规则层的测试：签名归一化、否定极性、余弦。无 DB 依赖，纯函数。"""

from acme.kw_enterprise.memory.dedup import content_tokens, cosine, has_negation, negation_conflict, signature


def test_signature_is_space_and_case_insensitive_but_keeps_negation():
    assert signature("  回答  用表格 ") == signature("回答用表格")
    assert signature("Use Tables") == signature("use  tables")
    # 标点不折叠：正反两句话是不同的签名（octop-memory 的防稀释立场）
    assert signature("我会用表格") != signature("我不会用表格")


def test_content_tokens_cjk_per_char_ascii_per_word():
    assert "回" in content_tokens("回答用表格")
    assert "tables" in content_tokens("use tables")
    # 停词滤掉，否定字保留
    assert "的" not in content_tokens("我的偏好")
    assert "不" in content_tokens("不用表格")


def test_negation_conflict_needs_overlap_and_opposite_polarity():
    assert negation_conflict("回答用表格输出", "以后不要用表格输出")  # 同话题 + 极性相反
    assert not negation_conflict("回答用表格输出", "回答用表格输出")  # 同极性不冲突
    assert not negation_conflict("回答用表格输出", "称呼用中文姓名")  # 不同话题
    assert not negation_conflict("用", "不用")  # 重叠不足 3 个 token 不判
    assert has_negation("以后不要用表格") and not has_negation("以后要用表格")


def test_cosine_matches_dimension_or_returns_zero():
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0
    assert cosine([1, 2, 3], [1, 2, 3]) > 0.99
    assert cosine([1, 0], [1, 0, 0]) == 0.0  # 维度不齐（换嵌入模型后的旧向量）
    assert cosine([], [1, 0]) == 0.0
