"""判重的纯规则层（设计 §5 第 3 步）：不花 LLM 的两道前置——签名精确去重、否定极性冲突。

借鉴 octop-memory 的晋升检查：
- 归一化**有意不剔除标点**——「我会用表格」和「我不会用表格」不是同一句；
- 否定冲突宁可保守：漏掉真冲突顶多多进一次待确认，误报会把「以后不用表格」判成「用表格」的反例丢掉。

余弦也放这里（与规则同层被 extract 与未来的整理复用）：每用户 ≤200 行，进程内算毫秒级。
"""

import re
import unicodedata
from math import sqrt

# 归一化签名：NFKC 折叠 + casefold + 移除全部空白。保留标点（见模块 docstring）——
# 空白整个去掉是因为 LLM 的表述在中英文间插入的空格没有语义（「回答 用表格」≡「回答用表格」）
_WS = re.compile(r"\s+")


def signature(content: str) -> str:
    return _WS.sub("", unicodedata.normalize("NFKC", content).casefold())


# 否定检测（M2 R1 审查定口径）：只认**明确的否定短语**，不做单字子串匹配——「不错」「不同」
# 「差不多」「没关系」「特别」这类高频词全是裸单字误伤面。宁可漏（漏掉的冲突有 LLM 判定与
# 待确认兜底），不把惯用肯定句误判成否定。
NEGATION_PHRASES = ("不用", "不要", "不能", "不会", "不想", "不喜欢", "不希望", "不可以", "不使用",
                    "不采用", "不需要", "不再", "不必", "没有", "暂不", "避免", "别用", "别再", "别发",
                    "don't", "won't", "isn't", "can't", "aren't", "needn't")

# 停词：算「内容 token 重叠」时滤掉，不参与语义比对。注意不能把否定词放进来
_STOP_CN = ("的 了 是 我 你 他 她 它 们 我们 你们 和 与 或 在 有 就 都 也 很 被 把 吧 呢 吗"
            " 啊 要 会 能 可以 这个 那个")
_STOP_EN = ("the", "a", "an", "is", "are", "was", "were", "to", "of", "and", "or", "in", "on",
            "for", "it", "its", "this", "that", "i", "you", "we", "they", "he", "she", "be")
STOP_TOKENS = frozenset(_STOP_CN.split()) | frozenset(_STOP_EN)

_ASCII = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")

# 共享 ≥3 个内容 token 才算「讨论同一话题」：保守设计，宁漏勿误（octop-memory 同款阈值）
OVERLAP_MIN = 3


def content_tokens(text: str) -> frozenset[str]:
    """英文按词、中文逐字；去停词。否定字/词保留在 token 里（它们是内容的一部分）。"""
    lowered = unicodedata.normalize("NFKC", text).casefold()
    tokens = set(_ASCII.findall(lowered)) | set(_CJK.findall(lowered))
    return frozenset(tokens - STOP_TOKENS)


def has_negation(text: str) -> bool:
    lowered = unicodedata.normalize("NFKC", text).casefold()
    if any(phrase in lowered for phrase in NEGATION_PHRASES):
        return True
    return bool(set(_ASCII.findall(lowered)) & _NEG_EN)  # 英文按词级匹配（not/never/no/avoid）


_NEG_EN = frozenset({"no", "not", "never", "avoid", "cannot"})


def negation_conflict(a: str, b: str) -> bool:
    """同话题 + 否定极性相反 → 疑似矛盾（直接进待确认，不劳烦 LLM 也不落生效）。"""
    shared = content_tokens(a) & content_tokens(b)
    return len(shared) >= OVERLAP_MIN and has_negation(a) != has_negation(b)


def cosine(a: list[float], b: list[float]) -> float:
    """维度不齐返回 0（换嵌入模型后的旧向量跳过相似度，整理任务懒重算补齐）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = sqrt(sum(x * x for x in a)) * sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
