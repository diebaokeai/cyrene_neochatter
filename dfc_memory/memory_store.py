"""cyrene_memory 存储与迁移。

数据模型：
  entries[]    语义记忆条目 {id,text,embedding,source,weight,createdAt,metadata}（迁移自 Cyrene rag-data）
  profile      L0/L1 长期画像
  relationship 关系/情绪/关怀提示列表
  worldbook[]  {name,triggers,resident,content} 触发式背景
"""

from __future__ import annotations

import json, os, re, time, uuid
from pathlib import Path
from typing import Any

_EMPTY: dict[str, Any] = {
    "entries": [],
    "profile": {},
    "relationship": [],
    "worldbook": [],
    "meta": {},
}


def default_source_dir() -> str:
    """Cyrene 用户数据目录（%APPDATA%/live2d-cyrene）。"""
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


def memory_path(base: str) -> Path:
    return Path(base) / "memory.json"


def load_memory(base: str) -> dict[str, Any]:
    data = _read_json(memory_path(base))
    if not isinstance(data, dict):
        data = dict(_EMPTY)
    for key, val in _EMPTY.items():
        data.setdefault(key, val)
    return data


def save_memory(base: str, data: dict[str, Any]) -> None:
    _write_json(memory_path(base), data)


def _parse_worldbook_md(path: Path) -> list[dict[str, Any]]:
    """解析 Cyrene worldbook md（块以 ## 开头，含 触发词/常驻 行）。"""
    text = path.read_text(encoding="utf-8")
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            if current is not None:
                entries.append(current)
            current = {"name": line[3:].strip(), "triggers": [], "resident": False, "lines": []}
            continue
        if current is None:
            continue
        if line.startswith("- 触发词") and ":" in line:
            raw_t = line.split(":", 1)[1]
            current["triggers"] = [t.strip() for t in re.split(r"[,，、]", raw_t) if t.strip()]
        elif line.startswith("- 常驻") and "是" in line:
            current["resident"] = True
        elif line:
            current["lines"].append(line)
    if current is not None:
        entries.append(current)
    out = []
    for e in entries:
        content = "\n".join(e["lines"]).strip()
        if content and (e["triggers"] or e["resident"]):
            out.append({"name": e["name"], "triggers": e["triggers"], "resident": bool(e["resident"]), "content": content[:2000]})
    return out


def migrate(source_dir: str, base: str, worldbook_dir: str | None = None) -> dict[str, Any]:
    """把 Cyrene 记忆迁移到本插件存储；已迁移则跳过。"""
    src = Path(source_dir)
    data = load_memory(base)
    if data.get("meta", {}).get("migrated"):
        # 已迁移但 WorldBook 缺失时，尝试从指定目录补迁（老数据升级路径）
        if not data.get("worldbook"):
            wb_fallback = Path(worldbook_dir) if worldbook_dir and Path(worldbook_dir).is_dir() else src / "prompts" / "worldbook"
            if wb_fallback.is_dir():
                wb_added = 0
                for md in sorted(wb_fallback.glob("*.md")):
                    for entry in _parse_worldbook_md(md):
                        entry["file"] = md.name
                        data["worldbook"].append(entry)
                        wb_added += 1
                if wb_added:
                    data["meta"]["worldbookMergedAt"] = time.time()
                    save_memory(base, data)
                    return {"skipped": "partial", "worldbook_merged": wb_added}
        return {"skipped": True}

    added = 0; rel = 0; wb = 0
    rag = _read_json(src / "rag-data" / "memory-store.json")
    if isinstance(rag, list):
        for item in rag:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            emb = item.get("embedding")
            entry = {
                "id": str(item.get("id") or f"cyrene_{uuid.uuid4().hex}"),
                "text": text,
                "source": str(item.get("source") or "cyrene"),
                "weight": float(item.get("weight") or 1.0),
                "createdAt": float(item.get("createdAt") or 0.0),
                "metadata": item.get("metadata") or {},
            }
            if isinstance(emb, list) and len(emb) >= 64:
                entry["embedding"] = [float(v) for v in emb]
            data["entries"].append(entry)
            added += 1

    mem = _read_json(src / "memory.json")
    if isinstance(mem, dict):
        profile: dict[str, Any] = {}
        l0 = mem.get("l0") if isinstance(mem.get("l0"), dict) else {}
        l1 = mem.get("l1") if isinstance(mem.get("l1"), dict) else {}
        for key in ("nickname", "preferredName", "occupation", "longTermInterests", "permanentNote"):
            val = str(l0.get(key) or "").strip()
            if val:
                profile[key] = val
        for key in ("recentGoals", "recentPreferences", "currentProject"):
            val = str(l1.get(key) or "").strip()
            if val:
                profile[key] = val
        data["profile"] = profile

    relog = _read_json(src / "relationship-log.json")
    entries_rel = relog.get("entries") if isinstance(relog, dict) else None
    if isinstance(entries_rel, list):
        kept = []
        for item in entries_rel[-400:]:
            if not isinstance(item, dict):
                continue
            rec = {
                "userText": str(item.get("userText") or ""),
                "date": str(item.get("date") or ""),
                "cyreneFeeling": str(item.get("cyreneFeeling") or ""),
                "userMood": str(item.get("userMood") or ""),
                "relationshipSignal": str(item.get("relationshipSignal") or ""),
                "importantMoment": str(item.get("importantMoment") or ""),
                "nextCareCue": str(item.get("nextCareCue") or ""),
            }
            kept.append(rec)
        data["relationship"] = kept
        rel = len(kept)

    wb_dir = Path(worldbook_dir) if worldbook_dir and Path(worldbook_dir).is_dir() else src / "prompts" / "worldbook"
    if wb_dir.is_dir():
        for md in sorted(wb_dir.glob("*.md")):
            for entry in _parse_worldbook_md(md):
                entry["file"] = md.name
                data["worldbook"].append(entry)
                wb += 1

    data["meta"] = {"migrated": True, "source": str(src), "migratedAt": time.time()}
    save_memory(base, data)
    return {"skipped": False, "entries": added, "relationship": rel, "worldbook": wb}


async def embed_text(text: str) -> list[float]:
    """用 MoFox embedding 任务（bge-m3）编码单条文本。"""
    from src.app.plugin_system.api.llm_api import create_embedding_request, get_model_set_by_task

    request = create_embedding_request(model_set=get_model_set_by_task("embedding"), request_name="cyrene_memory_embedding", inputs=[text])
    response = await request.send()
    embeddings = getattr(response, "embeddings", None) or []
    if not embeddings:
        raise RuntimeError("Embedding 请求返回为空")
    return [float(v) for v in embeddings[0]]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def recall(entries: list[dict[str, Any]], query_vec: list[float], top_k: int, min_score: float) -> list[dict[str, Any]]:
    """余弦召回 top-k；跳过贴纸等非文本条目。"""
    scored = []
    for e in entries:
        text = str(e.get("text") or "").strip()
        if not text or text.startswith("["):
            continue
        emb = e.get("embedding")
        if not isinstance(emb, list):
            continue
        score = cosine(query_vec, [float(v) for v in emb])
        if score >= min_score:
            scored.append((score, text, e.get("source", "")))
    scored.sort(key=lambda x: -x[0])
    return [{"score": s, "text": t, "source": src} for s, t, src in scored[:top_k]]


__all__ = ["default_source_dir", "load_memory", "save_memory", "migrate", "embed_text", "recall", "cosine"]