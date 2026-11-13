"""昔涟回复风格后处理：清理 AI 腔结构，不改变原意。"""

from __future__ import annotations

import re

_AI_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"作为(?:一个)?\s*AI[^。！？\n]*[。！？]?",
        r"我(?:很)?理解你的感受[^。！？\n]*[。！？]?",
        r"希望能(?:够)?帮到你[^。！？\n]*[。！？]?",
        r"总结(?:一下|来说)[：:，,]?",
        r"综上所述[，,]?",
        r"总之[，,]?",
        r"首先[，,]?",
        r"其次[，,]?",
        r"再者[，,]?",
        r"需要注意的是[，,]?",
        r"建议你[，,]?",
        r"友情提示[：:，,]?",
    )
)

_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?")


def _split_long(sentence: str, max_chars: int = 50) -> list[str]:
    """把过长的句子在逗号处拆开，避免一口气说太多。"""

    if len(sentence) <= max_chars:
        return [sentence]
    pieces: list[str] = []
    rest = sentence
    while len(rest) > max_chars:
        index = rest.find("，")
        while 0 <= index < 10:
            index = rest.find("，", index + 1)
        if index < 0 or index > max_chars + 12:
            break
        pieces.append(rest[:index].rstrip("，") + "。")
        rest = rest[index + 1 :]
    if rest:
        pieces.append(rest)
    return pieces


def polish(text: str) -> str:
    """清理 AI 汇报腔：去套话、拆冒号列举、切长句。"""

    value = str(text or "").strip()
    if not value:
        return value

    # 保护 URL，避免把 https:// 里的冒号改掉
    urls: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        urls.append(match.group(0))
        return f"\u0000URL{len(urls) - 1}\u0000"

    value = _URL_RE.sub(_stash, value)

    for pattern in _AI_PATTERNS:
        value = pattern.sub("", value)

    value = re.sub(r"[ \t]{2,}", " ", value)
    value = value.replace("：", "，").replace("；", "。")
    value = re.sub(r"^[，,、\s]+", "", value)

    cleaned: list[str] = []
    for match in _SENTENCE_RE.finditer(value):
        piece = match.group(0).strip()
        if not piece:
            continue
        cleaned.extend(_split_long(piece))

    result = "".join(cleaned).strip()
    for index, url in enumerate(urls):
        result = result.replace(f"\u0000URL{index}\u0000", url)
    return result
