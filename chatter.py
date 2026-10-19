"""Cyrene NeoChatter：以 F 桌为底的独立私聊引擎（v0.1 骨架）。

回合：读未读 → 组上下文（系统人格+触发记忆+心理流+未读）→ LLM 单轮生成
      → 按句分条发送 → 记心理流 → 节流自动写入记忆 → Wait。
说明：默认插件关闭；开启后在真机上按日志逐步打磨（事件/时序以重启实测为准）。
"""

from __future__ import annotations

import asyncio
import re
import time
import asyncio
from typing import Any, AsyncGenerator

from src.app.plugin_system.base import BaseChatter, Wait
from src.app.plugin_system.api.send_api import send_text, send_voice
from src.app.plugin_system.api.feature_switch_api import get_switch
from src.app.plugin_system.api.text_policy import contains_emoji, strip_emoji, strip_stage_parens, strip_tool_markup
from src.core.components.types import ChatType

from . import user_scope
from .config import CyreneNeoConfig
from .llm import Inference
from .memory import load_store, migrate, save_store
from .mental_log import MentalLog
from .persona import WORLDBOOK_DIR, assemble_system_persona, match_worldbook
from .pre_understanding import format_understanding, understand_turn

logger = None  # 由 plugin 注入避免 import 环；运行时惰性获取


def _get_logger():
    global logger
    if logger is None:
        from src.app.plugin_system.api.log_api import get_logger

        logger = get_logger("cyrene_neochatter")
    return logger


def _fmt(msg: Any) -> str:
    text = str(getattr(msg, "processed_plain_text", "") or str(getattr(msg, "content", "") or "")).strip()
    if not text:
        return ""
    name = str(getattr(msg, "sender_name", "") or "对方")
    if str(getattr(msg, "sender_role", "") or "").lower() == "bot":
        return ""
    return f"[{name}] {text[:400]}"


def _fmt_history(msg: Any) -> str:
    """历史消息格式化：双方都保留（用户/对方与你自己），用于多轮上下文注入。"""
    text = str(getattr(msg, "processed_plain_text", "") or str(getattr(msg, "content", "") or "")).strip()
    if not text:
        return ""
    if str(getattr(msg, "sender_role", "") or "").lower() == "bot":
        return "你：" + text[:300]
    name = str(getattr(msg, "sender_name", "") or "对方")
    return f"对方（{name}）：" + text[:300]


def _fill_profile_from_candidates(data: dict[str, Any], candidates: list[str]) -> int:
    """从抽取候选里补全 L0 画像的空字段，并用最新候选刷新 L1 近期备忘；返回改动数。

    只填空字段（不覆盖 WebUI/手工维护的内容）；称呼、特殊日期、长期偏好分别落到
    nickname/preferredName、permanentNote、longTermInterests。
    """
    if not candidates:
        return 0
    l0 = data.setdefault("l0", {})
    changed = 0
    for raw in candidates:
        text = str(raw or "").strip()
        if not text:
            continue
        snippet = text[:120]
        if any(k in text for k in ("叫", "称呼", "昵称", "小名", "别名")):
            prefers = any(k in text for k in ("被叫", "想被叫", "希望你叫", "想让你叫", "喜欢被你叫"))
            key = "preferredName" if prefers else "nickname"
            if not str(l0.get(key) or "").strip():
                name_value = snippet
                for prefix in (
                    "称呼对方为", "称呼对方是", "称呼", "对方喜欢被叫", "喜欢被叫",
                    "对方希望你叫", "希望你叫", "想让你叫", "想被叫", "被叫",
                    "对方叫", "对方是", "小名是", "小名", "别名是", "别名",
                    "昵称是", "昵称", "叫做", "叫",
                ):
                    if name_value.startswith(prefix):
                        name_value = name_value[len(prefix):]
                        break
                name_value = name_value.strip("：: 。，,")[:40]
                if "/" in name_value:
                    name_value = name_value.split("/")[0].strip()
                for filler in ("人家", "对方", "我", "你"):
                    if name_value.startswith(filler) and len(name_value) > len(filler):
                        name_value = name_value[len(filler):].strip()
                        break
                l0[key] = name_value or snippet
                changed += 1
        elif text.startswith(("对方是", "他是", "她是")) and len(text) <= 12:
            if not str(l0.get("nickname") or "").strip():
                name_value = text[3:].strip("：: 。，,")
                if "/" in name_value:
                    name_value = name_value.split("/")[0].strip()
                if name_value and not any(bad in name_value for bad in ("岁", "学生", "工作", "朋友", "同事")):
                    l0["nickname"] = name_value[:20]
                    changed += 1
        elif any(k in text for k in ("生日", "纪念日", "周年")):
            if not str(l0.get("permanentNote") or "").strip():
                l0["permanentNote"] = snippet
                changed += 1
        elif any(k in text for k in ("喜欢", "习惯", "每天", "总是", "一直")):
            current = str(l0.get("longTermInterests") or "").strip()
            if not current:
                l0["longTermInterests"] = snippet
                changed += 1
            elif snippet[:40] not in current:
                l0["longTermInterests"] = (current + "；" + snippet)[:300]
                changed += 1
    recent = "；".join(dict.fromkeys(str(c)[:60] for c in candidates if str(c).strip()))[:300]
    if recent and recent != str(data.get("l1") or "")[:300]:
        data["l1"] = recent
        changed += 1
    return changed




def _write_depth_layers(data: dict[str, Any], parsed: dict[str, Any], cfg: CyreneNeoConfig) -> int:
    """写入 l2 事件 / reflection / conflict 三层；返回写入条数。"""
    depth = getattr(cfg, "memory_depth", None)
    if depth is None or not isinstance(parsed, dict):
        return 0
    added = 0
    events = parsed.get("events")
    if isinstance(events, list) and bool(getattr(depth, "events_enabled", True)):
        for raw in events:
            if isinstance(raw, dict):
                text = str(raw.get("summary") or "").strip()
                date = str(raw.get("date") or "").strip()[:10] or time.strftime("%Y-%m-%d")
            else:
                text = str(raw or "").strip()
                date = time.strftime("%Y-%m-%d")
            if not text:
                continue
            data.setdefault("l2", []).append({
                "date": date,
                "summary": text[:200],
                "source": "auto",
                "createdAt": time.time(),
            })
            added += 1
        keep_events = int(getattr(depth, "events_keep", 300) or 300)
        if len(data.get("l2") or []) > keep_events:
            data["l2"] = data["l2"][-keep_events:]
    reflection = parsed.get("reflection")
    if isinstance(reflection, str) and reflection.strip() and bool(getattr(depth, "reflection_enabled", True)):
        data.setdefault("reflection", []).append({
            "date": time.strftime("%Y-%m-%d"),
            "text": reflection.strip()[:200],
            "source": "auto",
            "createdAt": time.time(),
        })
        added += 1
        keep_ref = int(getattr(depth, "reflection_keep", 100) or 100)
        if len(data.get("reflection") or []) > keep_ref:
            data["reflection"] = data["reflection"][-keep_ref:]
    conflict = parsed.get("conflict")
    if isinstance(conflict, dict) and bool(getattr(depth, "conflict_enabled", True)):
        old = str(conflict.get("old") or "").strip()
        new = str(conflict.get("new") or "").strip()
        if old or new:
            data.setdefault("conflict", []).append({
                "date": time.strftime("%Y-%m-%d"),
                "old": old[:150],
                "new": new[:150],
                "resolution": str(conflict.get("resolution") or "")[:150],
                "source": "auto",
                "createdAt": time.time(),
            })
            added += 1
        keep_conf = int(getattr(depth, "conflict_keep", 100) or 100)
        if len(data.get("conflict") or []) > keep_conf:
            data["conflict"] = data["conflict"][-keep_conf:]
    return added


class CyreneNeoChatter(BaseChatter):
    chatter_name: str = "cyrene_neochatter"
    chatter_description: str = "昔涟私聊引擎：分层记忆 + 心理活动流 + 等待与主动想起（F 桌样式）"
    associated_platforms: list[str] = []
    chat_type: ChatType = ChatType.PRIVATE
    dependencies: list[str] = []

    def __init__(self, stream_id: str, plugin: Any) -> None:
        super().__init__(stream_id=stream_id, plugin=plugin)
        self._cfg: CyreneNeoConfig | None = None
        self._mental: MentalLog | None = None
        self._infer: Inference | None = None

    def _config(self) -> CyreneNeoConfig:
        from .config import CyreneNeoConfig

        current = self.plugin.config if isinstance(self.plugin.config, CyreneNeoConfig) else CyreneNeoConfig()
        if self._cfg is None or self._cfg is not current:
            self._cfg = current
        return self._cfg

    @staticmethod
    def _feature_enabled(key: str, default: bool) -> bool:
        """运行时功能开关：优先读共享开关（WebUI 可切），缺省用配置默认值。"""
        try:
            return bool(get_switch(key, bool(default)))
        except Exception:
            return bool(default)

    async def _mem_base(self) -> str:
        """当前私聊对象的记忆目录（按 QQ 分库；解析不到时落到 _shared）。

        Returns:
            形如 data/cyrene_neochatter/users/3308747519 的目录字符串。
        """

        user_id = await user_scope.resolve_user_id(self.stream_id)
        return user_scope.scope_dir(
            self._config().memory.data_dir or "data/cyrene_neochatter", user_id
        )

    def _get_mental(self) -> MentalLog:
        if self._mental is None:
            self._mental = MentalLog(int(self._config().mental.max_log_entries))
        return self._mental

    def _get_infer(self) -> Inference:
        if self._infer is None:
            self._infer = Inference(self._config(), self.plugin)
        return self._infer

    async def _synthesize_voice(self, text: str) -> str | None:
        """把一句话合成为语音 base64。

        优先走进程内 TTS provider registry（tts_http_server），拿不到再回退到
        config 里的 HTTP 接口（voice_chatter 同款协议）。两者都不可用返回 None。
        """
        cfg = self._config()
        voice = getattr(cfg, "voice", None)
        provider = str(getattr(voice, "provider", "") or "").strip()
        # 1) 进程内服务（不依赖端口，provider 配置由 tts_http_server 决定）
        try:
            from src.app.plugin_system.api.service_api import get_service

            registry = get_service("tts_http_server:service:tts_provider_registry")
            if registry is not None:
                from plugins.tts_http_server.protocol import TTSSynthesisRequest

                options: dict[str, Any] = {}
                if provider:
                    options["provider"] = provider
                result = await registry.synthesize(
                    TTSSynthesisRequest(stream_id=self.stream_id, text=text, options=options)
                )
                audio_b64 = str(getattr(result, "audio_base64", "") or "")
                if audio_b64:
                    return audio_b64
        except Exception as exc:
            _get_logger().debug(f"TTS 服务调用失败，尝试 HTTP 回退: {exc}")
        # 2) HTTP 回退
        endpoint = str(getattr(voice, "endpoint", "") or "").strip()
        if not endpoint:
            return None
        try:
            import httpx

            payload: dict[str, Any] = {
                "protocol": "mfx-tts-http-v1",
                "stream_id": self.stream_id,
                "text": text,
                "markers": {},
                "options": {"mime_type": str(getattr(voice, "mime_type", "audio/wav") or "audio/wav")},
            }
            if provider:
                payload["options"]["provider"] = provider
            timeout = float(getattr(voice, "timeout", 30.0) or 30.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(endpoint, json=payload)
                resp.raise_for_status()
                data = resp.json()
            audio_b64 = str((data or {}).get("audio_base64") or "")
            return audio_b64 or None
        except Exception as exc:
            _get_logger().warning(f"TTS 合成失败（HTTP 回退）: {exc}")
            return None

    async def _send_voice_sentence(self, text: str) -> bool:
        """给一句话补发一条语音；任何失败都只记日志，不影响文字发送。"""
        cfg = self._config()
        voice = getattr(cfg, "voice", None)
        limit = int(getattr(voice, "max_chars", 120) or 120)
        if limit > 0 and len(text) > limit:
            _get_logger().debug(f"语音回复跳过：{len(text)} 字超过上限 {limit}")
            return False
        audio_b64 = await self._synthesize_voice(text)
        if not audio_b64:
            return False
        try:
            ok = bool(
                await send_voice(
                    voice_data=audio_b64,
                    stream_id=self.stream_id,
                    processed_plain_text=text,
                )
            )
        except Exception as exc:
            _get_logger().warning(f"语音发送异常: {exc}")
            return False
        if not ok:
            _get_logger().warning("语音发送失败（平台拒绝或格式不受支持）")
            return False
        _get_logger().info(f"语音已发送：{text[:24]}")
        return True

    _MEME_COOLDOWN_SECONDS = 90.0

    async def _auto_emoji_meme(self, chat_stream: Any, hint: str) -> None:
        """引擎正文里若出现内联 emoji，文本剥离后补发一张图片式表情包替代表达（限频）。

        emoji_sender 未启用 / 库为空时静默跳过，不影响正文发送。
        """
        plugin = self.plugin
        if plugin is None:
            return
        last_map = getattr(plugin, "_cnc_meme_last", None)
        if last_map is None:
            last_map = plugin._cnc_meme_last = {}
        if time.monotonic() - float(last_map.get(self.stream_id) or 0.0) < self._MEME_COOLDOWN_SECONDS:
            return
        try:
            from src.app.plugin_system.api.service_api import get_service

            service = get_service("emoji_sender:service:emoji_sender")
            if service is None or not hasattr(service, "send_best_detailed"):
                return
            query = (hint or "").strip()[:100] or "刚刚那种撒娇又带点小脾气的情绪"
            ok, _result, reason = await service.send_best_detailed(
                stream_id=self.stream_id,
                platform=getattr(chat_stream, "platform", None),
                description_query=query,
                emotion_tags=None,
            )
            last_map[self.stream_id] = time.monotonic()
            if ok:
                _get_logger().info("cyrene_neochatter: 已为内联 emoji 补发图片表情包")
            else:
                _get_logger().debug(f"cyrene_neochatter: 自动表情包未发送: {reason}")
        except Exception as exc:
            _get_logger().debug(f"cyrene_neochatter: 自动表情包补发异常: {exc}")

    async def _context(self) -> tuple[Any, Any]:
        from src.core.managers.stream_manager import get_stream_manager

        sm = get_stream_manager()
        chat_stream = await sm.activate_stream(self.stream_id)
        context = getattr(chat_stream, "context", None)
        return chat_stream, context

    async def _user_allowed(self) -> bool:
        """名单为空=放行；否则仅命中名单内用户。"""
        cfg = self._config()
        allowed = {str(u).strip() for u in (cfg.general.user_ids or []) if str(u).strip()}
        if not allowed:
            return True
        user_id = await user_scope.resolve_user_id(self.stream_id)
        return bool(user_id and user_id in allowed)

    async def _ensure_memory(self) -> dict[str, Any]:
        cfg = self._config()
        base = await self._mem_base()
        data = load_store(base)
        if not data.get("meta", {}).get("migrated") and cfg.memory.auto_migrate:
            source = (cfg.memory.source_dir or "").strip() or __import__("os").environ.get("APPDATA", "") + "/live2d-cyrene"
            try:
                migrate(source, base, str(WORLDBOOK_DIR))
                data = load_store(base)
            except Exception as exc:
                _get_logger().warning(f"迁移失败（不影响运行）: {exc}")
        return data

    async def _auto_write(self, user_text: str, data: dict[str, Any]) -> None:
        cfg = self._config()
        base = await self._mem_base()
        rel_added = 0
        entry_added = 0
        dup_skipped = 0
        try:
            transcript = "当前时间：" + current_time_text() + "。" + chr(10) + user_text[:1800]
            parsed = await _extract_memory(self._get_infer(), cfg, transcript)
            rel = parsed.get("relationship")
            candidates: list[str] = []
            raw_candidates = parsed.get("memory_candidates")
            if isinstance(raw_candidates, list):
                for item in raw_candidates:
                    text_item = str(item or "").strip()
                    if text_item and text_item not in candidates:
                        candidates.append(text_item)
            legacy_candidate = str(parsed.get("memory_candidate") or "").strip()
            if legacy_candidate and legacy_candidate not in candidates:
                candidates.append(legacy_candidate)
            if isinstance(rel, dict) and cfg.memory_write.write_relationship and (rel.get("importantMoment") or rel.get("nextCareCue") or rel.get("userMood")):
                data.setdefault("relationship", []).append({
                    "userText": str(rel.get("userText") or "")[:120],
                    "userMood": str(rel.get("userMood") or ""),
                    "relationshipSignal": str(rel.get("relationshipSignal") or ""),
                    "importantMoment": str(rel.get("importantMoment") or ""),
                    "nextCareCue": str(rel.get("nextCareCue") or ""),
                    "date": time.strftime("%Y-%m-%d"),
                    "source": "auto",
                })
                rel_added = 1
            if candidates and cfg.memory_write.write_entries:
                from .memory import similar_to_existing

                for candidate in candidates:
                    text_item = candidate[: int(cfg.memory_write.max_entry_chars)]
                    if not text_item:
                        continue
                    emb = None
                    try:
                        emb = await self._get_infer().embed(text_item)
                    except Exception:
                        emb = None
                    if emb is not None and similar_to_existing(data, emb, float(cfg.memory_write.dedupe_min_score)):
                        dup_skipped += 1
                        continue
                    entry: dict[str, Any] = {
                        "id": "auto_" + str(time.time_ns()),
                        "text": text_item,
                        "source": "auto",
                        "weight": 1.0,
                        "createdAt": time.time(),
                        "metadata": {},
                    }
                    if emb:
                        entry["embedding"] = emb
                    data.setdefault("entries", []).append(entry)
                    entry_added += 1
            profile_added = _fill_profile_from_candidates(data, candidates)
            depth_added = _write_depth_layers(data, parsed, cfg)
            if rel_added or entry_added or profile_added or depth_added:
                save_store(base, data)
                _get_logger().info(
                    f"cyrene_neochatter 自动写入完成: relationship+{rel_added}, entries+{entry_added}, "
                    f"画像+{profile_added}, 深度+{depth_added}, 去重跳过={dup_skipped}, path={base}/memory.json"
                )
            else:
                _get_logger().info(
                    f"cyrene_neochatter 自动写入检查: 本回合无可记内容（relationship 无信号 / 候选为空或去重={dup_skipped}），未写入 path={base}/memory.json"
                )
        except Exception as exc:
            _get_logger().warning(f"自动写入失败: {exc}")

    async def _build_memory_extra(self, data: dict[str, Any], haystack: str) -> str:
        cfg = self._config()
        sections: list[str] = []
        NL = chr(10)
        l0 = data.get("l0") or {}
        if cfg.recall.profile and (l0 or data.get("l1")):
            lines = []
            label = {"nickname": "称呼", "preferredName": "喜欢被叫", "occupation": "身份", "longTermInterests": "兴趣", "permanentNote": "一直想记住的"}
            for k, v in l0.items():
                if isinstance(v, str) and v.strip():
                    lines.append("- " + label.get(k, k) + "：" + v[:120])
            if data.get("l1"):
                lines.append("- 近期备忘：" + str(data["l1"])[:300])
            if lines:
                sections.append("## 关于对方（长期记忆）" + NL + NL.join(lines))
        care = ""
        for rec in reversed(data.get("relationship") or []):
            cue = str(rec.get("nextCareCue") or "").strip()
            if cue and "不要过度解读" not in cue:
                care = "- 关怀备忘：" + cue[:200]
                break
        if cfg.recall.relationship_care and care:
            sections.append("## 记得关心" + NL + care)
        if cfg.recall.semantic:
            try:
                infer = self._get_infer()
                qv = await infer.embed(haystack[:600])
                top_k = int(cfg.recall.top_k)
                rerank_task = str(getattr(cfg.llm, "rerank_task", "") or "").strip()
                hits: list[dict[str, Any]] = []
                if rerank_task and self._feature_enabled("cyrene_rerank", True):
                    wide = max(top_k * int(getattr(cfg.recall, "rerank_multiplier", 4) or 4), top_k)
                    wide = min(wide, 20)
                    pre_score = float(getattr(cfg.recall, "prefilter_min_score", 0.05) or 0.0)
                    candidates = _recall_local(data, qv, wide, min(pre_score, float(cfg.recall.min_score)))
                    docs = [str(item.get("text") or "") for item in candidates]
                    if docs:
                        scores = await infer.rerank(haystack[:600], docs, top_n=top_k)
                        if scores and len(scores) == len(candidates):
                            ranked = sorted(zip(scores, candidates), key=lambda pair: pair[0], reverse=True)
                            hits = [item for _score, item in ranked[:top_k]]
                        else:
                            hits = candidates[:top_k]
                if not hits:
                    hits = _recall_local(data, qv, top_k, float(cfg.recall.min_score))
                if hits:
                    lines = ["- " + str(h.get("text"))[: int(cfg.recall.per_text_chars)] for h in hits]
                    sections.append("## 相关的旧记忆（只作参考，未列出的过去不要编造）" + NL + NL.join(lines))
            except Exception:
                pass
        if cfg.recall.worldbook and data.get("worldbook"):
            hits = match_worldbook(data["worldbook"], haystack, int(cfg.recall.worldbook_max))
            if hits:
                sections.append("## 可能相关的背景（按需理解，不要主动背诵）" + NL + NL.join(hits))
        depth = getattr(cfg, "memory_depth", None)
        if depth is not None and bool(getattr(depth, "events_enabled", False)):
            keep = int(getattr(depth, "inject_recent_events", 5) or 0)
            event_lines = []
            for item in (data.get("l2") or [])[-keep:] if keep > 0 else []:
                if isinstance(item, dict):
                    text = str(item.get("summary") or "").strip()
                    date = str(item.get("date") or "")[:10]
                    if text:
                        event_lines.append("- " + (date + " " if date else "") + text[:180])
            if event_lines:
                sections.append("## 最近的经历（真实发生过，按时间）" + NL + NL.join(event_lines))
        if depth is not None and bool(getattr(depth, "reflection_enabled", False)):
            keep_r = int(getattr(depth, "inject_reflections", 2) or 0)
            ref_lines = []
            for item in (data.get("reflection") or [])[-keep_r:] if keep_r > 0 else []:
                text = str(item.get("text") if isinstance(item, dict) else item or "").strip()
                if text:
                    ref_lines.append("- " + text[:200])
            if ref_lines:
                sections.append("## 我的反思" + NL + NL.join(ref_lines))
        if depth is not None and bool(getattr(depth, "conflict_enabled", False)):
            keep_c = int(getattr(depth, "inject_conflicts", 2) or 0)
            conflict_lines = []
            for item in (data.get("conflict") or [])[-keep_c:] if keep_c > 0 else []:
                if isinstance(item, dict):
                    old = str(item.get("old") or "").strip()
                    new = str(item.get("new") or "").strip()
                    note = str(item.get("resolution") or "").strip()
                    if old or new:
                        conflict_lines.append("- 旧：" + old[:80] + " → 新：" + new[:80] + (("（" + note[:80] + "）") if note else ""))
            if conflict_lines:
                sections.append("## 需要注意的变化（以最新事实为准）" + NL + NL.join(conflict_lines))
        return NL + NL + NL.join(sections)

    async def _update_history_summary(self, cfg: CyreneNeoConfig, older_lines: list[str], existing: str) -> str:
        """把更早的历史与已有摘要合并成滚动摘要（失败则保留原摘要）。"""
        if not older_lines:
            return existing
        task = str(getattr(cfg.context, "summary_task", "utils_small") or "utils_small")
        max_chars = int(getattr(cfg.context, "summary_max_chars", 600) or 600)
        try:
            from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
            from src.kernel.llm import LLMPayload, ROLE, Text

            request = create_llm_request(
                get_model_set_by_task(task),
                request_name="cyrene_neochatter_history_summary",
            )
            request.add_payload(LLMPayload(ROLE.SYSTEM, Text(
                "你是对话记忆压缩器。把【已有摘要】与【更早的对话】合并为一段客观、精简的滚动摘要："
                "保留人物、事件、承诺、偏好、未完成事项；用第三人称；不要编造；"
                "不超过 " + str(max_chars) + " 字，直接输出摘要正文。"
            )))
            request.add_payload(LLMPayload(ROLE.USER, Text(
                "【已有摘要】" + chr(10) + (existing or "（无）") + chr(10) + chr(10)
                + "【更早的对话】" + chr(10) + NL_JOIN(older_lines)
            )))
            response = await asyncio.wait_for(request.send(stream=False), timeout=20.0)
            await asyncio.wait_for(response, timeout=20.0)
            text = str(getattr(response, "message", "") or "").strip()
            return text[:max_chars] if text else existing
        except Exception as exc:
            _get_logger().debug(f"历史摘要失败（回退旧摘要）: {exc}")
            return existing

    async def _build_history_block(
        self,
        context: Any,
        cfg: CyreneNeoConfig,
        data: dict[str, Any],
    ) -> tuple[str, list[str]]:
        """按 token 预算选取最近历史，超出部分滚动摘要；返回（注入块, 选中的历史行）。"""
        NL = chr(10)
        limit = int(getattr(cfg.general, "history_limit", 12) or 0)
        if limit <= 0:
            return "", []
        msgs = list(getattr(context, "history_messages", None) or [])
        lines = [_fmt_history(m) for m in msgs[-limit:]]
        lines = [line for line in lines if line]
        if not lines:
            return "", []
        budget = int(
            int(getattr(cfg.context, "token_budget", 3000) or 0)
            * float(getattr(cfg.context, "chars_per_token", 1.8) or 1.8)
        )
        budget = max(200, budget)
        selected: list[str] = []
        used = 0
        for line in reversed(lines):
            if not selected or used + len(line) <= budget:
                selected.append(line)
                used += len(line)
            else:
                break
        selected.reverse()
        older = lines[: len(lines) - len(selected)]
        parts: list[str] = []
        summary = str(data.get("history_summary") or "").strip()
        if older and self._feature_enabled("cyrene_history_summary", bool(getattr(cfg.context, "summary_enabled", True))):
            new_summary = await self._update_history_summary(cfg, older, summary)
            if new_summary and new_summary != summary:
                data["history_summary"] = new_summary
                try:
                    save_store(await self._mem_base(), data)
                except Exception:
                    pass
                summary = new_summary
        if summary:
            parts.append("更早的对话摘要：" + summary)
        if selected:
            parts.append(
                "之前的对话（按时间顺序，仅供理解语境，不要复述或引用）：" + NL + NL_JOIN(selected)
            )
        if not parts:
            return "", selected
        return NL.join(parts) + NL + NL, selected

    async def execute(self) -> AsyncGenerator[Any, Any]:
        cfg = self._config()
        if not cfg.general.enabled:
            return
        cadence = getattr(self.plugin, "_cnc_turn_count", 0)
        while True:
            try:
                chat_stream, context = await self._context()
                if context is None:
                    resume = yield Wait()
                    continue
                if cfg.general.user_ids and not await self._user_allowed():
                    resume = yield Wait()
                    continue
                unreads = list(getattr(context, "unread_messages", None) or [])
                if not unreads:
                    resume = yield Wait()
                    continue
                lines = [fmt for fmt in (_fmt(m) for m in unreads) if fmt]
                if not lines:
                    try:
                        context.unread_messages.clear()
                    except Exception:
                        pass
                    resume = yield Wait()
                    continue
                activity = getattr(self.plugin, "_note_stream_activity", None)
                if callable(activity):
                    try:
                        activity(self.stream_id)
                    except Exception:
                        pass
                user_text = NL_JOIN(lines)
                haystack = user_text
                data = await self._ensure_memory() if cfg.memory.enabled else load_store(await self._mem_base())
                extra = await self._build_memory_extra(data, haystack) if cfg.memory.enabled else ""
                system = assemble_system_persona(cfg) + extra
                NL = chr(10)
                history_block, history_used = await self._build_history_block(context, cfg, data)
                understanding_block = ""
                if self._feature_enabled("cyrene_understanding", bool(getattr(cfg.understanding, "enabled", True))):
                    info = await understand_turn(self._get_infer(), cfg, history_used, user_text)
                    understanding_block = format_understanding(info)
                user_block = (
                    "现在时间：" + current_time_text() + "。" + NL + NL
                    + (understanding_block + NL + NL if understanding_block else "")
                    + history_block
                    + "对方刚才对你说：" + NL + user_text + NL + NL
                    + "（以上只是让你知道发生了什么；回复时直接接自己的话，不要复述、引用或点破对方的原话。）"
                )
                mental = self._get_mental().render()
                if mental and "还没有内心记录" not in mental:
                    user_block = user_block + "\n\n你的内心活动（可继续接上）：\n" + mental
                reply = (await self._get_infer().chat(system, user_block) or "").strip()
                if not reply:
                    _get_logger().warning("引擎首次返回空回复（可能为模型偶发空输出），自动重试一次")
                    reply = (await self._get_infer().chat(system, user_block) or "").strip()
                if not reply:
                    _get_logger().warning("引擎返回空回复（重试后仍为空），本回合跳过发送")
                    try:
                        context.flush_unreads_to_history()
                    except Exception:
                        pass
                    continue
                persona_break = _breaks_persona(reply)
                if persona_break:
                    _get_logger().warning(f"回复出现出戏内容（{persona_break}），按昔涟口吻重写一次")
                    retry_block = user_block + NL + NL + "（刚才那版说的话会让对方出戏，请完全以昔涟的口吻，重新自然地说一遍你现在想说的话。）"
                    reply = (await self._get_infer().chat(system, retry_block) or "").strip()
                    if not reply:
                        _get_logger().warning("重写后仍为空，本回合跳过发送")
                        try:
                            context.flush_unreads_to_history()
                        except Exception:
                            pass
                        continue
                segments = _split_reply_sentences(reply)
                if not segments:
                    segments = [reply.strip()]
                meme_hint = ""
                sent_n = 0
                first_sent = True
                # 语音回复：热加载开关（功能开关面板 cyrene_voice_reply）+ 配置默认值
                voice_cfg = getattr(cfg, "voice", None)
                voice_on = bool(
                    voice_cfg is not None
                    and self._feature_enabled("cyrene_voice_reply", bool(getattr(voice_cfg, "enabled", False)))
                )
                voice_only = voice_on and str(getattr(voice_cfg, "mode", "after_text") or "after_text") == "voice_only"
                for seg in segments:
                    had_emoji = contains_emoji(seg)
                    clean = strip_tool_markup(seg)
                    clean = strip_stage_parens(clean)
                    clean = strip_emoji(clean)
                    if not clean:
                        continue
                    if had_emoji and not meme_hint:
                        meme_hint = clean
                    if not first_sent:
                        await asyncio.sleep(_send_pace_seconds(clean))
                    first_sent = False
                    # voice_only：先尝试只发语音，失败自动退回文字
                    if voice_only and await self._send_voice_sentence(clean):
                        sent_n += 1
                        continue
                    try:
                        ok_sent = await send_text(content=clean, stream_id=self.stream_id, platform=getattr(chat_stream, "platform", None))
                        if ok_sent:
                            sent_n += 1
                    except Exception as exc:
                        _get_logger().warning(f"发送失败: {exc}")
                    # after_text：文字发完后补一条语音
                    if voice_on and not voice_only:
                        await self._send_voice_sentence(clean)
                if meme_hint:
                    await self._auto_emoji_meme(chat_stream, meme_hint)
                if sent_n == 0:
                    _get_logger().warning("引擎回复清洗后无可发送内容（可能只含工具标记/emoji），本回合跳过发送")
                    try:
                        context.flush_unreads_to_history()
                    except Exception:
                        pass
                    continue
                preview = segments[0] if segments else reply
                preview = (strip_emoji(preview) or preview)[:160]
                self._get_mental().push(preview, mood="")
                try:
                    context.flush_unreads_to_history()
                except Exception:
                    pass
                cadence += 1
                setattr(self.plugin, "_cnc_turn_count", cadence)
                if cfg.memory.enabled and cfg.memory_write.enabled and cadence % max(1, int(cfg.memory_write.interval_turns)) == 0:
                    try:
                        await self._auto_write(user_text, data)
                    except Exception as exc:
                        _get_logger().warning(f"自动写入异常: {exc}")
            except Exception as exc:
                _get_logger().warning(f"引擎回合异常: {exc}")
            resume = yield Wait()


_PERSONA_BREAK_MARKERS = (
    "作为AI", "作为人工智能", "作为语言模型", "作为一个语言模型",
    "我是AI", "我是机器人", "我是一个语言模型", "人工智能语言模型",
    "系统提示词", "提示词注入", "<invoke", "tx-sticker", "sticker_",
)


def _breaks_persona(text: str) -> str:
    """检测回复里明显“出戏”的说法，返回命中的原因（无则空串）。"""
    for marker in _PERSONA_BREAK_MARKERS:
        if marker in text:
            return marker
    return ""


def NL_JOIN(lines: list[str]) -> str:
    """用换行符连接多条格式化消息行。"""
    return chr(10).join(lines)


_WEEKDAYS = "一二三四五六日"


def current_time_text() -> str:
    """返回本地当前时间文本，如 2026-09-05 16:49（周六）。"""
    lt = time.localtime()
    return time.strftime("%Y-%m-%d %H:%M", lt) + "（周" + _WEEKDAYS[lt.tm_wday] + "）"


def _send_pace_seconds(text: str) -> float:
    """句间模拟人类输入的间隔：约 0.8s + 每字 0.12s，下限 1.2s、上限 4.0s。"""
    return max(1.2, min(4.0, 0.8 + 0.12 * len(text)))


def _split_reply_sentences(reply: str) -> list[str]:
    """把回复切成“一条完整的话”用于逐句发送。

    边界规则：
    - 句末标点（。！？!?；;）后切分；
    - 特殊尾缀（～♪♫…⋯）连同紧随的装饰符吸收到本句末尾后切分；
    - 半角句号连续 3 个及以上视为省略号结尾；
    - 空行（段落分隔）为天然分句点，普通换行按空格处理。
    """
    NL = chr(10)
    CR = chr(13)
    text = reply.replace(CR + NL, NL).replace(CR, NL)
    boundary = "。！？!?；;～♪♫…⋯"
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)
    strong_end = "。！？!?；;～♪♫…⋯."
    merge_min = 14

    def flush() -> None:
        seg = "".join(buf).strip()
        if seg:
            out.append(seg)
        buf.clear()

    while i < n:
        ch = text[i]
        if ch == NL:
            j = i
            while j < n and text[j] == NL:
                j += 1
            if j - i >= 2:
                # 空行（段落断点）：若当前积压仍是「短句且无强结尾」的诗行，
                # 先不打断，与下一段拼成一句完整的话再发，避免消息切太细
                pending = "".join(buf).strip()
                if pending and len(pending) < merge_min and pending[-1] not in strong_end:
                    if buf and buf[-1] != " ":
                        buf.append(" ")
                else:
                    flush()
            elif buf and buf[-1] != " ":
                buf.append(" ")  # 普通换行折为空格
            i = j
            continue
        buf.append(ch)
        if ch in boundary:
            j = i + 1
            while j < n and text[j] in boundary:
                buf.append(text[j])
                j += 1
            flush()
            i = j
            continue
        if ch == ".":
            j = i + 1
            while j < n and text[j] == ".":
                j += 1
            if j - i >= 3:
                buf.extend(text[i + 1:j])
                flush()
                i = j
                continue
        i += 1
    flush()
    return out


def _recall_local(data: dict[str, Any], qv: list[float], top_k: int, min_score: float) -> list[dict[str, Any]]:
    from .memory import recall

    return recall(data, qv, top_k, min_score)


async def _extract_memory(infer: Inference, cfg: CyreneNeoConfig, transcript: str) -> dict[str, Any]:
    """抽取关系/值得记住的话（使用 memory_write.task）。"""
    from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
    from src.kernel.llm import LLMPayload, ROLE, Text

    task = getattr(cfg.memory_write, "task", "utils_small") or "utils_small"
    request = create_llm_request(get_model_set_by_task(task), request_name="cyrene_neochatter_write")
    request.add_payload(LLMPayload(ROLE.SYSTEM, Text(
        "你是昔涟的记忆整理助手。读取一段私聊记录，严格输出一个 JSON（不要多余文字）："
        '{"relationship": {...}|null, "memory_candidates": ["一句话"], "events": [{"date": "YYYY-MM-DD", "summary": "..."}], "reflection": "..."|null, "conflict": {"old": "...", "new": "...", "resolution": "..."}|null}。'
        "规则："
        "1) relationship：只要对方表达了情绪（开心、委屈、撒娇、生气、想念、疲惫、不安、害羞等）就【必须】给出；"
        "字段 userText（对方原话，可截断）、userMood（情绪词）、relationshipSignal（这透露了什么）、"
        "importantMoment（重要时刻/边界/约定，没有就空串）、nextCareCue（下次如何回应或关怀，一句话）。"
        "没有情绪时 relationship 为 null。"
        "2) memory_candidates：数组，每条一句话，只写值得长期记住的稳定信息；"
        "以下类型【必须】写进去：持续型偏好或习惯（出现「每天/以后/一直/总是/每次都」、喜欢被怎样对待、"
        "不喜欢什么、想听/想看/想被叫等长期愿望）、称呼与昵称、身份与关系事实、"
        "特殊日期（生日/纪念日/约定日期；能结合给出的当前时间换算成具体日期就写具体日期）、"
        "长期目标或约定、其他稳定事实。没有这类信息时给空数组 []；普通寒暄、单次玩笑不要写。"
        "3) events：数组（可为空），只记录这轮真实发生的具体事件（做了什么、约定了什么、发生了什么）；"
        "每条 {date: YYYY-MM-DD, summary: 一句话}；没有就 []。"
        "4) reflection：字符串或 null。如果这轮让你对对方/关系有了新的理解、或该调整的相处方式，写成一句第一人称反思；没有就 null。"
        "5) conflict：对象或 null。如果本轮信息与之前记忆矛盾（例如以前说不熬夜如今在熬夜、称呼/喜好变了），"
        "给出 {old: 旧的事实, new: 新的事实, resolution: 以后以什么为准}；没有矛盾就 null。"
        "6) 只依据记录，不编造；不确定就不写。"
    )))
    request.add_payload(LLMPayload(ROLE.USER, Text(transcript)))
    response = await request.send(stream=False)
    await response
    raw = str(getattr(response, "message", "") or "")
    import json

    text = raw.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            import json_repair

            parsed = json_repair.loads(m.group(0))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}
