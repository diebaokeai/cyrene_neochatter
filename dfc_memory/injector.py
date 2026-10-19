"""cyrene_memory 事件注入器（私聊只读上线；群聊不注入，保持原记忆系统）。"""

from __future__ import annotations

from typing import Any, cast

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BaseEventHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision

from .. import user_scope
from ..config import CyreneConfig
from . import memory_store

logger = get_logger("cyrene_neochatter.dfc_memory.injector")


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _build_profile_section(profile: dict[str, Any]) -> str:
    """把 L0/L1 画像素描成一行行描述。"""
    lines = []
    label = {"nickname": "怎么称呼", "preferredName": "喜欢被叫", "occupation": "身份", "longTermInterests": "长期兴趣", "permanentNote": "一直想记住的", "recentGoals": "近期目标", "recentPreferences": "近期偏好", "currentProject": "在忙的事"}
    for key, text in profile.items():
        val = str(text or "").strip()
        if val:
            lines.append("- " + label.get(key, key) + "：" + val)
    return "\n".join(lines) if lines else ""


def _build_care_section(relationship: list[dict[str, Any]]) -> str:
    """取最近一条带有效关怀提示的关系记录。"""
    for item in reversed(relationship):
        cue = str(item.get("nextCareCue") or "").strip()
        if cue and not cue.startswith("下次回应提示：延续最近话题") and "不要过度解读" not in cue:
            return "- 关怀备忘：" + _clip(cue, 200)
    return ""


def _match_worldbook(worldbook: list[dict[str, Any]], haystack: str, limit: int) -> list[str]:
    matched = []
    for entry in worldbook:
        if entry.get("resident"):
            matched.append(entry)
            continue
        triggers = entry.get("triggers") or []
        if any(t and t in haystack for t in triggers):
            matched.append(entry)
    selected = matched[:limit]
    out = []
    for e in selected:
        name = str(e.get("name") or "背景")
        out.append("- 【" + name + "】" + _clip(str(e.get("content") or ""), 420))
    return out


class CyreneMemoryInjector(BaseEventHandler):
    """在用户提示词构建时（私聊）注入画像 / 召回记忆 / 关怀提示 / 触发背景。"""

    handler_name: str = "cyrene_memory_injector"
    handler_description: str = "私聊注入长期记忆（画像/召回/关系/背景），群聊放行"
    weight: int = 22
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [EventType.ON_PROMPT_BUILD]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        cfg = self._config()
        if cfg is None or not cfg.dfc_memory.enabled:
            return EventDecision.PASS, params

        name = str(params.get("name") or "")
        if name not in cfg.memory_templates.user_prompts:
            return EventDecision.PASS, params

        values = params.get("values")
        if not isinstance(values, dict):
            return EventDecision.PASS, params

        chat_type = str(values.get("chat_type") or "").strip().lower()
        # NFC 兼容桥：NFC 触发 on_prompt_build 时 values 无 chat_type 但含 content，视为私聊
        nfc_bridge = bool(cfg.memory_scope.nfc_bridge) and "content" in values and not chat_type
        # 作用域：仅 私聊(private / NFC私聊桥) 启用完整记忆；群聊/临时会话(discuss) 保持原记忆系统
        is_private = chat_type == "private" or nfc_bridge
        allow_group = bool(cfg.memory_scope.group_enabled) and chat_type in ("group", "discuss")
        if not (is_private or allow_group):
            return EventDecision.PASS, params
        if is_private and not cfg.memory_scope.private_enabled:
            return EventDecision.PASS, params
        # user_ids 非空 = 仅对名单内用户生效
        # 私聊对象（QQ）：既是白名单依据，也决定记忆分库目录
        scope_user_id = await user_scope.resolve_user_id_from_values(values) if is_private else ""
        if is_private and cfg.memory_scope.user_ids:
            allowed = {str(item).strip() for item in cfg.memory_scope.user_ids if str(item).strip()}
            if not scope_user_id or scope_user_id not in allowed:
                return EventDecision.PASS, params

        base = user_scope.scope_dir(
            cfg.memory_store.data_dir or "data/cyrene_memory", scope_user_id
        )
        data = memory_store.load_memory(base)
        sections: list[str] = []

        # 1) 画像 L0/L1
        if cfg.memory_inject.profile:
            profile_text = _build_profile_section(data.get("profile") or {})
            if profile_text:
                sections.append("## 关于伙伴（长期记忆）\n" + profile_text)

        # 2) 关怀提示（关系记忆）
        if cfg.memory_inject.care_cue:
            care = _build_care_section(data.get("relationship") or [])
            if care:
                sections.append("## 记得关心\n" + care)

        # 3) 语义召回
        if cfg.memory_inject.recall and data.get("entries"):
            query_text = (str(values.get("unreads") or "") + " " + str(values.get("extra") or ""))[:600]
            if query_text.strip():
                try:
                    query_vec = await memory_store.embed_text(query_text)
                    hits = memory_store.recall(data.get("entries") or [], query_vec, cfg.memory_inject.top_k, float(cfg.memory_inject.min_score))
                    if hits:
                        lines = ["- " + _clip(h["text"], cfg.memory_inject.per_text_chars) for h in hits]
                        sections.append("## 相关的旧记忆（只作参考，未列出的过去不要自行编造）\n" + "\n".join(lines))
                except Exception as exc:
                    logger.debug(f"cyrene_memory 召回失败，跳过本轮: {exc}")

        # 4) WorldBook 触发背景（仅私聊）
        if cfg.memory_inject.worldbook and is_private and data.get("worldbook"):
            haystack = str(values.get("history") or "") + "\n" + str(values.get("unreads") or "") + "\n" + str(values.get("extra") or "")
            hits = _match_worldbook(data.get("worldbook") or [], haystack, int(cfg.memory_inject.worldbook_max))
            if hits:
                sections.append("## 可能相关的背景（按需理解，不要主动背诵）\n" + "\n".join(hits))

        if not sections:
            return EventDecision.PASS, params

        block = "\n\n".join(sections)
        if len(block) > int(cfg.memory_inject.total_chars):
            block = block[: int(cfg.memory_inject.total_chars)]

        inject_mode = str(getattr(cfg.memory_inject, "inject_mode", "extra") or "extra")
        if inject_mode == "append_template":
            template = str(params.get("template") or "")
            if not template:
                return EventDecision.PASS, params
            params["template"] = template.rstrip() + "\n\n" + block
        else:
            existing_extra = str(values.get("extra") or "").strip()
            values["extra"] = existing_extra + "\n\n" + block if existing_extra else block
            params["values"] = values
        if cfg.dfc_memory.debug_log:
            logger.debug(f"cyrene_memory: 已向 {name} 注入记忆板块 ({len(block)} chars)")
        return EventDecision.SUCCESS, params

    def _config(self) -> CyreneConfig | None:
        if not self.plugin or not self.plugin.config:
            return None
        return cast(CyreneConfig, self.plugin.config)


__all__ = ["CyreneMemoryInjector"]