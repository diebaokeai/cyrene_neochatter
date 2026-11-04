"""核心 API 兼容层。

较旧的 Neo-MoFox 核心没有 ``text_policy`` 与 ``feature_switch_api`` 模块，
直接导入会导致插件加载失败。这里优先使用核心实现，缺失时退回本地等价实现，
让插件在旧核心上也能运行。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# ── 正文文本策略 ─────────────────────────────────────────────
try:  # 新核心
    from src.app.plugin_system.api.text_policy import (  # type: ignore
        contains_emoji,
        strip_emoji,
        strip_stage_parens,
        strip_tool_markup,
    )
except Exception:  # 旧核心：本地等价实现
    def _chr_range(start: int, end: int) -> str:
        return chr(start) + "-" + chr(end)

    _EMOJI_SYMBOLS = (
        _chr_range(0x1F000, 0x1FAFF)
        + _chr_range(0x1F1E6, 0x1F1FF)
        + _chr_range(0x2600, 0x27BF)
        + _chr_range(0x2B00, 0x2BFF)
        + _chr_range(0x2190, 0x21FF)
        + chr(0xFE0E)
        + chr(0xFE0F)
        + chr(0x200D)
        + chr(0x20E3)
    )
    _EMOJI_RE = re.compile("[" + _EMOJI_SYMBOLS + "]+")
    _KEEP_RE = re.compile("[" + chr(0x266A) + chr(0x266B) + "]")
    _ELEMENT_RE = re.compile("<[^<>]+>[^<>]*?</[^<>]+>", re.DOTALL)
    _TAG_ONLY_RE = re.compile("<[^<>]+>")

    def _collapse_hspace(text: str) -> str:
        separator = chr(10)
        return separator.join(" ".join(line.split()) for line in text.split(separator))

    def _cut_trailing_paren_group(text: str) -> tuple[str, bool]:
        t = text.rstrip()
        if not t.endswith("）"):
            return text, False
        open_idx = t.rfind("（")
        if open_idx < 0 or open_idx == len(t) - 1:
            return text, False
        inner = t[open_idx + 1:-1]
        if "（" in inner or "）" in inner:
            return text, False
        if len(inner) > 80 or "://" in inner or "http" in inner:
            return text, False
        if not inner.strip():
            return text, False
        return t[:open_idx].rstrip(), True

    def _cut_leading_paren_group(text: str) -> tuple[str, bool]:
        t = text.lstrip()
        if not t.startswith("（"):
            return text, False
        close_idx = t.find("）")
        if close_idx <= 0:
            return text, False
        inner = t[1:close_idx]
        if "（" in inner or "）" in inner:
            return text, False
        if len(inner) > 80 or "://" in inner or "http" in inner:
            return text, False
        if not inner.strip():
            return text, False
        return t[close_idx + 1:].lstrip(), True

    def strip_stage_parens(text: object) -> str:
        if not text:
            return str(text or "")
        s = str(text)
        for _ in range(4):
            s, changed1 = _cut_trailing_paren_group(s)
            s, changed2 = _cut_leading_paren_group(s)
            if not changed1 and not changed2:
                break
        return s.strip()

    def strip_tool_markup(text: object) -> str:
        if not text:
            return str(text or "")
        cleaned = _ELEMENT_RE.sub(" ", str(text))
        cleaned = _TAG_ONLY_RE.sub(" ", cleaned)
        return _collapse_hspace(cleaned).strip()

    def contains_emoji(text: object) -> bool:
        if not text:
            return False
        return _EMOJI_RE.search(_KEEP_RE.sub("", str(text))) is not None

    def strip_emoji(text: object) -> str:
        if not text:
            return str(text or "")
        source = str(text)
        saved: list[str] = []

        def _stash(match: "re.Match[str]") -> str:
            saved.append(match.group(0))
            return chr(0xE000 + len(saved) - 1)

        protected = _KEEP_RE.sub(_stash, source)
        cleaned = _EMOJI_RE.sub("", protected)
        for index, value in enumerate(saved):
            cleaned = cleaned.replace(chr(0xE000 + index), value)
        return _collapse_hspace(cleaned).strip()


# ── 跨插件功能开关 ───────────────────────────────────────────
try:  # 新核心
    from src.app.plugin_system.api.feature_switch_api import (  # type: ignore
        get_switch,
        set_switch,
    )
except Exception:  # 旧核心：本地 JSON 存储
    _STORE_PATH = Path("data/feature_switches.json")
    _cache: dict[str, Any] | None = None

    def _load_switch_store() -> dict[str, Any]:
        global _cache
        if _cache is None:
            try:
                raw = _STORE_PATH.read_text(encoding="utf-8") if _STORE_PATH.is_file() else "{}"
                data = json.loads(raw)
            except Exception:
                data = {}
            _cache = data if isinstance(data, dict) else {}
        return _cache

    def _save_switch_store() -> None:
        try:
            _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _STORE_PATH.write_text(
                json.dumps(_cache or {}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def get_switch(name: str, default: bool = False) -> bool:
        return bool(_load_switch_store().get(str(name), default))

    def set_switch(name: str, value: bool) -> bool:
        _load_switch_store()[str(name)] = bool(value)
        _save_switch_store()
        return bool(value)


__all__ = [
    "contains_emoji",
    "strip_emoji",
    "strip_stage_parens",
    "strip_tool_markup",
    "get_switch",
    "set_switch",
]
