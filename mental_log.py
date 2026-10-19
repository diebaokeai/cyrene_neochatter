"""心理活动流（MentalLog）：会话内第一人称内心记录。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class MentalLog:
    def __init__(self, max_entries: int = 60) -> None:
        self._max = max(5, int(max_entries))
        self._items: list[dict[str, Any]] = []

    def push(self, thought: str, mood: str = "", expected_reaction: str = "") -> None:
        self._items.append({
            "t": time.time(),
            "thought": str(thought or "")[:400],
            "mood": str(mood or ""),
            "expected": str(expected_reaction or "")[:160],
        })
        if len(self._items) > self._max:
            del self._items[: len(self._items) - self._max]

    def render(self) -> str:
        lines = []
        for item in self._items:
            parts = []
            if item.get("thought"):
                parts.append("内心：" + item["thought"])
            if item.get("mood"):
                parts.append("心情：" + item["mood"])
            if item.get("expected"):
                parts.append("预期对方反应：" + item["expected"])
            if parts:
                lines.append(" - ".join(parts))
        return "\n".join(lines) if lines else "（还没有内心记录）"

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._items)
