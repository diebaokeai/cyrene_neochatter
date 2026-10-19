"""persona/ 资源加载器（带缓存）。"""

from __future__ import annotations

from pathlib import Path

_PERSONA_DIR = Path(__file__).resolve().parent / "persona"


class CyrenePersonaStore:
    """按文件名读取 persona/*.md 并缓存。"""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    def read(self, name: str) -> str:
        """读取 persona 下文件内容；缺失时返回空字符串。"""
        if name in self._cache:
            return self._cache[name]
        text = ""
        path = _PERSONA_DIR / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
        self._cache[name] = text
        return text

    def style(self, filename: str) -> str:
        """读取 persona/styles/<filename>。"""
        if not filename or filename.endswith("/") or ".." in filename:
            return ""
        return self.read("styles/" + filename)

    def all_cached_names(self) -> list[str]:
        return sorted(self._cache)


_global_store: CyrenePersonaStore | None = None


def get_cyrene_persona_store() -> CyrenePersonaStore:
    global _global_store
    if _global_store is None:
        _global_store = CyrenePersonaStore()
    return _global_store
