"""cyrene 自动记忆写入器（Cyrene 式，独立于 booku_memory）。

订阅 AFTER_CHATTER_STEP：仅在“你的私聊”中，按节流节奏把本轮互动自动沉淀为：
  1) relationship：情绪/重要时刻/下次关怀提示；
  2) entries：值得长期记住的话（bge-m3 向量化 + 相似去重）。
数据只写 data/cyrene_memory，不依赖/不写入 booku_memory。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, cast

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BaseEventHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision

from .. import user_scope
from ..config import CyreneConfig
from . import memory_store

logger = get_logger("cyrene_neochatter.dfc_memory.writer")

_MAX_REL = 1000


async def _stream_chat_type(stream_id: str) -> str:
    """解析流的聊天类型（private/group…）。"""
    try:
        from src.core.managers.stream_manager import get_stream_manager

        info = await get_stream_manager().get_stream_info(stream_id)
        return str(info.get("chat_type") if isinstance(info, dict) else getattr(info, "chat_type", "") or "").strip().lower()
    except Exception:
        return ""


async def _extract(plugin: Any, cfg: CyreneConfig, transcript: str) -> dict[str, Any]:
    """调用 LLM 从最近互动中抽取关系信号与值得记住的话。"""
    from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
    from src.kernel.llm import LLMPayload, ROLE, Text

    request = create_llm_request(get_model_set_by_task(cfg.dfc_memory_write.task), request_name="cyrene_memory_write")
    request.add_payload(
        LLMPayload(
            ROLE.SYSTEM,
            Text(
                "你是昔涟的记忆整理助手。从下面的私聊记录里抽取两样东西，严格只输出一个 JSON：\n"
                "1) relationship：是否出现值得记住的情绪信号或重要时刻；没有则为 null。字段：userText(对方原话节选不超过60字)、userMood、relationshipSignal、importantMoment、nextCareCue。\n"
                "2) memory_candidate：如果对话里出现值得长期记住的稳定信息（对方偏好/习惯/身份事实/与你的约定），用一句话概括；否则为 null。普通寒暄、一次性闲聊、撒娇玩笑不要写。\n"
                '输出格式：{"relationship": {...} 或 null, "memory_candidate": "..." 或 null}。不要 Markdown、不要解释。'
            ),
        )
    )
    request.add_payload(LLMPayload(ROLE.USER, Text(transcript)))
    response = await request.send(stream=False)
    await response
    return _parse_json(str(getattr(response, "message", "") or ""))


def _parse_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            import json_repair

            data = json_repair.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _transcript(context: Any, limit: int = 8) -> str:
    """取最近上下文里用户消息的紧凑文本，避免写入时无内容。"""
    lines: list[str] = []
    messages = []
    for attr in ("unread_messages", "history_messages"):
        got = getattr(context, attr, None) or []
        if got:
            messages.extend(got)
    for msg in messages[-limit:]:
        if str(getattr(msg, "sender_role", "") or "").lower() == "bot":
            continue
        text = str(getattr(msg, "processed_plain_text", "") or str(getattr(msg, "content", "") or "")).strip()
        if text and not text.startswith("["):
            lines.append("[用户] " + text[:200])
    return "\n".join(lines)


async def _embed_safe(text: str) -> list[float] | None:
    try:
        return await memory_store.embed_text(text)
    except Exception as exc:
        logger.debug(f"embedding 失败，该条不带向量保存: {exc}")
        return None


def _similar(embedding: list[float], data: dict[str, Any], threshold: float) -> bool:
    for e in data.get("entries") or []:
        emb = e.get("embedding")
        if isinstance(emb, list) and len(emb) == len(embedding):
            if memory_store.cosine(embedding, [float(v) for v in emb]) >= threshold:
                return True
    return False


class CyreneMemoryWriter(BaseEventHandler):
    """私聊自动记忆写入器（仅对你的私聊生效）。"""

    handler_name: str = "cyrene_memory_writer"
    handler_description: str = "私聊自动沉淀关系/语义记忆（Cyrene 式，独立于 booku）"
    weight: int = 3
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [EventType.AFTER_CHATTER_STEP]

    def _config(self) -> CyreneConfig | None:
        if not self.plugin or not self.plugin.config:
            return None
        return cast(CyreneConfig, self.plugin.config)

    async def execute(self, event_name: str, params: dict[str, Any]) -> tuple[EventDecision, dict[str, Any]]:
        cfg = self._config()
        if cfg is None or not cfg.dfc_plugin.enabled or not cfg.dfc_memory.enabled or not cfg.dfc_memory_write.enabled:
            return EventDecision.PASS, params
        try:
            stream_id = str(params.get("stream_id") or "").strip()
            context = params.get("context")
            if not stream_id or context is None:
                return EventDecision.PASS, params

            chat_type = await _stream_chat_type(stream_id)
            if chat_type != "private":
                return EventDecision.PASS, params
            # 私聊对象（QQ）：既是白名单依据，也决定记忆分库目录
            scope_user_id = await user_scope.resolve_user_id(stream_id)
            allowed = {str(u).strip() for u in (cfg.memory_scope.user_ids or []) if str(u).strip()}
            if allowed and (not scope_user_id or scope_user_id not in allowed):
                return EventDecision.PASS, params

            state = getattr(self.plugin, "_cy_mem_write_state", None)
            if not isinstance(state, dict):
                state = {}
                setattr(self.plugin, "_cy_mem_write_state", state)
            cur = state.get(stream_id) or {"count": 0, "last": 0.0}
            cur["count"] += 1
            now = time.time()
            if cur["count"] < int(cfg.dfc_memory_write.interval_turns) or (now - cur["last"]) < float(cfg.dfc_memory_write.min_interval_seconds):
                state[stream_id] = cur
                return EventDecision.PASS, params
            cur["count"] = 0
            cur["last"] = now
            state[stream_id] = cur

            text = _transcript(context)
            if len(text.strip()) < 4:
                return EventDecision.PASS, params
            parsed = await _extract(self.plugin, cfg, text[:1800])
            rel = parsed.get("relationship")
            candidate = str(parsed.get("memory_candidate") or "").strip()
            if not rel and not candidate:
                return EventDecision.PASS, params

            base = user_scope.scope_dir(
                cfg.memory_store.data_dir or "data/cyrene_memory", scope_user_id
            )
            data = memory_store.load_memory(base)
            written_rel = written_entry = False
            if isinstance(rel, dict) and cfg.dfc_memory_write.enable_relationship:
                rec = {
                    "userText": str(rel.get("userText") or "")[:120],
                    "userMood": str(rel.get("userMood") or ""),
                    "relationshipSignal": str(rel.get("relationshipSignal") or ""),
                    "importantMoment": str(rel.get("importantMoment") or ""),
                    "nextCareCue": str(rel.get("nextCareCue") or ""),
                    "date": time.strftime("%Y-%m-%d"),
                    "source": "auto",
                }
                if rec.get("importantMoment") or rec.get("nextCareCue") or rec.get("userMood"):
                    rels = data.setdefault("relationship", [])
                    rels.append(rec)
                    if len(rels) > _MAX_REL:
                        del rels[: len(rels) - _MAX_REL]
                    written_rel = True
            if candidate and cfg.dfc_memory_write.enable_entries:
                candidate = candidate[: int(cfg.dfc_memory_write.max_entry_chars)]
                emb = await _embed_safe(candidate)
                if emb is None or not _similar(emb, data, float(cfg.dfc_memory_write.dedupe_min_score)):
                    entry: dict[str, Any] = {
                        "id": "auto_" + str(int(time.time() * 1000)),
                        "text": candidate,
                        "source": "auto",
                        "weight": 1.0,
                        "createdAt": time.time(),
                        "metadata": {"writer": "cyrene"},
                    }
                    if emb:
                        entry["embedding"] = emb
                    data.setdefault("entries", []).append(entry)
                    written_entry = True
            if written_rel or written_entry:
                memory_store.save_memory(base, data)
                logger.info(f"cyrene_memory: 自动写入 rel={written_rel} entry={written_entry}")
        except Exception as exc:
            logger.warning(f"cyrene_memory 自动写入失败: {exc}")
        return EventDecision.PASS, params


__all__ = ["CyreneMemoryWriter"]