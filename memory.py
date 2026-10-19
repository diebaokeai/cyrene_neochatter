"""cyrene_neochatter 分层记忆（F 桌样式，独立存储，不依赖 booku/cyrene）。

结构：l0 画像 / l1 滚动摘要 / l2 事件 / evidence / reflection / conflict /
     relationship（每轮关系）/ entries（语义条目，含向量）/ worldbook / meta
迁移源：%APPDATA%/live2d-cyrene（rag-data、memory.json、relationship-log.json）
      及自带 worldbook/ 目录。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .llm import cosine


def default_source_dir() -> str:
    appdata = os.environ.get("APPDATA", "")
    return str(Path(appdata) / "live2d-cyrene") if appdata else ""


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def empty_store() -> dict[str, Any]:
    return {
        "l0": {},
        "l1": "",
        "l2": [],
        "evidence": [],
        "reflection": [],
        "conflict": [],
        "relationship": [],
        "entries": [],
        "worldbook": [],
        "meta": {},
    }


def store_path(base: str) -> Path:
    return Path(base) / "memory.json"


def load_store(base: str) -> dict[str, Any]:
    data = _read_json(store_path(base))
    if not isinstance(data, dict):
        data = empty_store()
    for key, val in empty_store().items():
        data.setdefault(key, val)
    return data


def save_store(base: str, data: dict[str, Any]) -> None:
    _write_json(store_path(base), data)


# ── WorldBook md 解析（与 F 桌 prompts/worldbook 格式一致）──────────
def parse_worldbook_dir(directory: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not directory.is_dir():
        return entries
    for md in sorted(directory.glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        current: dict[str, Any] | None = None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("## "):
                if current is not None:
                    _flush(current, entries, md.name)
                current = {"name": line[3:].strip(), "triggers": [], "resident": False, "lines": []}
                continue
            if current is None:
                continue
            if line.startswith("- 触发词") and ":" in line:
                current["triggers"] = [t.strip() for t in re.split(r"[,，、]", line.split(":", 1)[1]) if t.strip()]
            elif line.startswith("- 常驻") and "是" in line:
                current["resident"] = True
            elif line:
                current["lines"].append(line)
        if current is not None:
            _flush(current, entries, md.name)
    return entries


def _flush(block: dict[str, Any], entries: list[dict[str, Any]], filename: str) -> None:
    content = "\n".join(block["lines"]).strip()
    if content and (block["triggers"] or block["resident"]):
        entries.append({"name": block["name"], "triggers": block["triggers"], "resident": bool(block["resident"]), "content": content[:2000], "file": filename})


# ── 迁移 ─────────────────────────────────────────────────────────
def _flatten_value(value: Any) -> str:
    """把 F 桌记忆值递归压成便于提示词阅读的短文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [s for v in value if (s := _flatten_value(v))]
        return "、".join(parts)
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            s = _flatten_value(v)
            if s:
                parts.append(f"{k}：{s}")
        return "；".join(parts)
    return str(value)


def _memory_l1_text(raw: Any) -> str:
    """F 桌 memory.json 的 l1 是对象（recentGoals/recentPreferences/currentProject/...），
    压成一条可读滚动摘要；若本来就是字符串则原样保留。"""
    if isinstance(raw, str):
        return raw.strip()
    if not isinstance(raw, dict):
        return ""
    parts = []
    for k, v in raw.items():
        if k in ("generatedAt", "roundCount", "version", "schemaVersion"):
            continue
        s = _flatten_value(v)
        if s:
            parts.append(f"{k}：{s}")
    text = "；".join(parts)[:2000]
    return text


def _repair_migrated_fields(src: Path, data: dict[str, Any]) -> dict[str, bool | list[str]]:
    """已迁移过的数据做幂等修复：旧解析器把 l1 dict 存成了 Python repr、且
    reflection/conflict 用了错误的键名，这里在 meta.migrated 分支里按源文件重算。"""
    repaired = {"l1": False, "fields": []}
    mem = _read_json(src / "memory.json")
    if not isinstance(mem, dict):
        return repaired
    new_l1 = _memory_l1_text(mem.get("l1"))
    old_l1 = str(data.get("l1") or "")
    # 旧解析器产物形状（str(dict) 或含内部键），或本地为空但源有内容时覆盖
    is_junk = old_l1.startswith("{'") or "generatedAt" in old_l1 or "roundCount" in old_l1
    if (is_junk or (not old_l1 and new_l1)) and new_l1 != old_l1:
        data["l1"] = new_l1
        repaired["l1"] = True
    for src_key, dst_key in (
        ("reflectionLogs", "reflection"),
        ("conflictLogs", "conflict"),
        ("evidence", "evidence"),
    ):
        src_val = mem.get(src_key)
        if isinstance(src_val, list) and src_val and not data.get(dst_key):
            data[dst_key] = src_val
            repaired["fields"].append(dst_key)  # type: ignore[union-attr]
    return repaired


def migrate(source_dir: str, base: str, bundled_worldbook: str | None = None) -> dict[str, Any]:
    src = Path(source_dir)
    data = load_store(base)
    if data.get("meta", {}).get("migrated"):
        merged = False
        if not data.get("worldbook"):
            wb_dir = Path(bundled_worldbook) if bundled_worldbook and Path(bundled_worldbook).is_dir() else Path(__file__).resolve().parent / "worldbook"
            for e in parse_worldbook_dir(wb_dir):
                data["worldbook"].append(e)
            data["meta"]["worldbookMergedAt"] = time.time()
            merged = True
        repaired = _repair_migrated_fields(src, data)
        if merged or repaired["l1"] or repaired["fields"]:
            data["meta"]["lastRepairedAt"] = time.time()
            save_store(base, data)
            return {
                "skipped": "partial",
                "worldbook_merged": len(data["worldbook"]) if merged else 0,
                "l1_repaired": bool(repaired["l1"]),
                "fields_repaired": list(repaired["fields"] or []),
            }
        return {"skipped": True}

    added = wb = 0
    rag = _read_json(src / "rag-data" / "memory-store.json")
    if isinstance(rag, list):
        for item in rag:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            entry: dict[str, Any] = {
                "id": str(item.get("id") or f"c_{uuid.uuid4().hex[:8]}"),
                "text": text,
                "source": str(item.get("source") or "cyrene"),
                "weight": float(item.get("weight") or 1.0),
                "createdAt": float(item.get("createdAt") or 0.0),
                "metadata": item.get("metadata") or {},
            }
            emb = item.get("embedding")
            if isinstance(emb, list) and len(emb) >= 64:
                entry["embedding"] = [float(v) for v in emb]
            data["entries"].append(entry)
            added += 1

    mem = _read_json(src / "memory.json")
    if isinstance(mem, dict):
        l0 = mem.get("l0") if isinstance(mem.get("l0"), dict) else {}
        data["l0"] = {k: v for k, v in l0.items() if isinstance(v, (str, bool)) and v not in ("", None)}
        data["l1"] = _memory_l1_text(mem.get("l1"))
        if isinstance(mem.get("l2"), list):
            data["l2"] = mem["l2"]
        # 真实文件键名为 reflectionLogs/conflictLogs（兼容旧的 reflection/conflict 命名）
        for src_key, dst_key in (
            ("evidence", "evidence"),
            ("reflectionLogs", "reflection"),
            ("conflictLogs", "conflict"),
            ("reflection", "reflection"),
            ("conflict", "conflict"),
        ):
            if isinstance(mem.get(src_key), list):
                data[dst_key] = mem[src_key]

    relog = _read_json(src / "relationship-log.json")
    entries_rel = relog.get("entries") if isinstance(relog, dict) else None
    if isinstance(entries_rel, list):
        kept = []
        for item in entries_rel[-400:]:
            if not isinstance(item, dict):
                continue
            kept.append({
                "userText": str(item.get("userText") or ""),
                "date": str(item.get("date") or ""),
                "userMood": str(item.get("userMood") or ""),
                "relationshipSignal": str(item.get("relationshipSignal") or ""),
                "importantMoment": str(item.get("importantMoment") or ""),
                "nextCareCue": str(item.get("nextCareCue") or ""),
            })
        data["relationship"] = kept

    wb_dir = Path(bundled_worldbook) if bundled_worldbook and Path(bundled_worldbook).is_dir() else Path(__file__).resolve().parent / "worldbook"
    for e in parse_worldbook_dir(wb_dir):
        data["worldbook"].append(e)
        wb += 1

    data["meta"] = {"migrated": True, "source": str(src), "migratedAt": time.time()}
    save_store(base, data)
    return {"entries": added, "relationship": len(data["relationship"]), "worldbook": wb}


# ── 召回 / 写入 ──────────────────────────────────────────────────
def recall(data: dict[str, Any], query_vec: list[float], top_k: int, min_score: float) -> list[dict[str, Any]]:
    scored = []
    for e in data.get("entries") or []:
        text = str(e.get("text") or "").strip()
        emb = e.get("embedding")
        if not text or text.startswith("[") or not isinstance(emb, list):
            continue
        score = cosine(query_vec, [float(v) for v in emb])
        if score >= min_score:
            scored.append((score, text, e.get("source", "")))
    scored.sort(key=lambda x: -x[0])
    return [{"score": s, "text": t, "source": src} for s, t, src in scored[:top_k]]


def similar_to_existing(data: dict[str, Any], embedding: list[float], threshold: float) -> bool:
    for e in data.get("entries") or []:
        emb = e.get("embedding")
        if isinstance(emb, list) and len(emb) == len(embedding):
            if cosine(embedding, [float(v) for v in emb]) >= threshold:
                return True
    return False
