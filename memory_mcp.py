"""Memory MCP：独立的记忆捕获 / 检索 / 统计服务。

设计目标：聊天模型只负责说人话，记忆候选以单条形式交给本 MCP，
避免一次生成一大坨 JSON 造成注意力分散。MCP 负责落库、去重提示与统计。

存储：
- 候选写入同一 SQLite（data/booku_memory/metadata.db）里的 memory_mcp_candidates 表；
- 检索同时查 booku_memory_records 与候选表；
- 不直接修改 booku 的正式记录，后续可由整合任务晋升/合并。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "booku_memory" / "metadata.db"

mcp = FastMCP("memory")

_TTL_BY_KIND = {
    "event": 14,
    "preference": 180,
    "person": 365,
    "knowledge": 365,
    "procedure": 365,
    "roleplay": 30,
}

_IMPORTANCE_HINTS = (
    "约定",
    "承诺",
    "生日",
    "纪念日",
    "喜欢",
    "讨厌",
    "边界",
    "称呼",
    "名字",
    "生病",
    "住院",
)


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        create table if not exists memory_mcp_candidates (
            id integer primary key autoincrement,
            text text not null,
            kind text not null default 'event',
            scope text not null default 'stream',
            importance real not null default 0.5,
            confidence real not null default 0.6,
            ttl_days integer not null default 14,
            persona_eligible integer not null default 0,
            reason text not null default '',
            status text not null default 'pending',
            created_at real not null
        )
        """
    )
    return con


def _score_importance(text: str) -> float:
    score = 0.35
    hits = sum(1 for hint in _IMPORTANCE_HINTS if hint in text)
    score += min(0.5, hits * 0.1)
    if len(text) > 40:
        score += 0.05
    return round(min(1.0, score), 2)


@mcp.tool()
def memory_capture(
    text: str,
    kind: str = "event",
    scope: str = "stream",
    importance: float | None = None,
    confidence: float | None = None,
    ttl_days: int | None = None,
    persona_eligible: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    """记录一条记忆候选（单条调用，不要批量）。

    Args:
        text: 一句话描述，尽量客观、可检索。
        kind: event / preference / person / knowledge / procedure / roleplay
        scope: global / stream / roleplay
        importance: 0~1；留空按关键词与长度估算。
        confidence: 0~1；留空默认 0.6。
        ttl_days: 过期天数；留空按 kind 默认。
        persona_eligible: 是否允许进入角色长期人设。
        reason: 为什么值得记。
    """

    clean = " ".join(str(text or "").split()).strip()
    if not clean:
        return {"ok": False, "error": "text 不能为空"}
    normalized_kind = str(kind or "event").strip().lower() or "event"
    imp = _score_importance(clean) if importance is None else max(0.0, min(1.0, float(importance)))
    conf = 0.6 if confidence is None else max(0.0, min(1.0, float(confidence)))
    ttl = _TTL_BY_KIND.get(normalized_kind, 14) if ttl_days is None else max(1, int(ttl_days))

    con = _connect()
    try:
        cur = con.execute(
            """insert into memory_mcp_candidates
               (text, kind, scope, importance, confidence, ttl_days, persona_eligible, reason, status, created_at)
               values (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (clean, normalized_kind, str(scope or "stream"), imp, conf, ttl, 1 if persona_eligible else 0, str(reason or ""), time.time()),
        )
        con.commit()
        return {
            "ok": True,
            "id": cur.lastrowid,
            "kind": normalized_kind,
            "importance": imp,
            "confidence": conf,
            "ttl_days": ttl,
            "persona_eligible": bool(persona_eligible),
        }
    finally:
        con.close()


@mcp.tool()
def memory_search(query: str, top_k: int = 5, include_candidates: bool = True) -> dict[str, Any]:
    """按关键词检索已有记忆与候选。"""

    q = str(query or "").strip()
    if not q:
        return {"ok": False, "error": "query 不能为空"}
    limit = max(1, min(50, int(top_k or 5)))
    like = f"%{q}%"
    con = _connect()
    try:
        hits = [
            {
                "source": "booku",
                "id": row["memory_id"],
                "title": row["title"],
                "kind": row["memory_type"],
                "content": (row["content"] or "")[:300],
            }
            for row in con.execute(
                """select memory_id, title, memory_type, content from booku_memory_records
                   where title like ? or content like ?
                   order by updated_at desc limit ?""",
                (like, like, limit),
            )
        ]
        if include_candidates:
            hits.extend(
                {
                    "source": "candidate",
                    "id": row["id"],
                    "title": "",
                    "kind": row["kind"],
                    "content": row["text"],
                }
                for row in con.execute(
                    """select id, kind, text from memory_mcp_candidates
                       where text like ? order by created_at desc limit ?""",
                    (like, limit),
                )
            )
        return {"ok": True, "query": q, "count": len(hits), "items": hits}
    finally:
        con.close()


@mcp.tool()
def memory_stats() -> dict[str, Any]:
    """返回候选与正式记忆的数量统计。"""

    con = _connect()
    try:
        pending = con.execute("select count(*) from memory_mcp_candidates where status='pending'").fetchone()[0]
        total = con.execute("select count(*) from booku_memory_records").fetchone()[0]
        by_kind = {
            row["kind"]: row["n"]
            for row in con.execute("select kind, count(*) as n from memory_mcp_candidates group by kind")
        }
        return {"ok": True, "booku_records": total, "pending_candidates": pending, "candidates_by_kind": by_kind}
    finally:
        con.close()


if __name__ == "__main__":
    mcp.run()
