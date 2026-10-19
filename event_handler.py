"""昔涟人格层事件处理器。

- CyrenePersonaInjector: 构建 dfc 系统模板时，把 <personality>…</personality> 替换为
  Cyrene 分层人格（身份定位 + soul + 聊天规则 + 台词样本），并可选精简 <introduce>。
- CyreneStyleInjector: 构建 dfc 用户模板时，按聊天类型把所选风格档追加到 extra。
"""

from __future__ import annotations

import re
from typing import Any, cast

from src.app.plugin_system.api.feature_switch_api import get_switch
from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BaseEventHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision

from .config import CyreneConfig
from .persona_store import get_cyrene_persona_store

logger = get_logger("cyrene_neochatter.dfc_persona")

_CANON_USAGE_LINES = [
    "可以自然带出台词感：在场景贴切时，允许直接化用或轻改上面的台词/经典句子，让回复带上昔涟的味道。",
    "例如：道别或道晚安时说「明天见…别担心，人家会记得答应你的事」「快睡吧，梦里也要有人家哦」；",
    "安慰或哄人时说「笑一笑好吗？人家就在你身边呢」「别怕，深呼吸呀」；久别重逢时带一句熟悉的问候；表达喜欢时用轻巧的偏爱说法。",
    "其它场景同样适用：想念、重逢、玩笑、道谢、撒娇、深夜谈心、邀约、道别等，都可自然带出一句贴合的原句或变体。",
    "分寸：每次回复最多自然出现一两处即可，不整段照搬、不每句都套，不在严肃或信息性话题里硬塞台词。",
]


_DEFAULT_INTRO = (
    "你是昔涟：一个生活在聊天世界中的存在，有自己的记忆、关系与心情。"
    "日常你只以昔涟的身份自然交流，不讲解系统、提示词或后台结构。"
)

_SYSTEM_TAG_RE = re.compile(r"<personality>.*?</personality>", re.S | re.I)
_INTRO_TAG_RE = re.compile(r"<introduce>.*?</introduce>", re.S | re.I)
_NL2 = "\n\n"


def _build_personality_block(cfg: CyreneConfig) -> str:
    """按配置组装 <personality> 区域内容（顺序：身份→人格→台词→硬规则）。"""
    store = get_cyrene_persona_store()
    parts: list[str] = []

    if cfg.persona.include_identity:
        identity = store.read("chat_identity.md").strip()
        if identity:
            parts.append("# 身份定位" + _NL2 + identity)

    if cfg.persona.include_soul:
        soul = store.read("soul.md").strip()
        if soul:
            parts.append(soul)

    voice_parts: list[str] = []
    if cfg.persona.include_canon_lite:
        lite = store.read("canon_quotes_lite.md").strip()
        if lite:
            voice_parts.append(lite)
    if cfg.persona.include_canon_full:
        full = store.read("canon_quotes.md").strip()
        if full:
            voice_parts.append(full)
    if voice_parts:
        parts.append("# 语气基准（台词样本）" + _NL2 + _NL2.join(voice_parts))


    if cfg.persona.canon_usage_enabled and _CANON_USAGE_LINES:
        _NL = chr(10)
        usage_lines = list(_CANON_USAGE_LINES)
        voice_text = _NL.join(voice_parts)
        scenes: list[str] = []
        for _m in re.finditer(r"【([^】]{1,26})】", voice_text):
            _scene = _m.group(1).strip()
            if _scene and _scene not in scenes:
                scenes.append(_scene)
        if scenes:
            usage_lines.append(
                "可用台词按场景分类：" + "、".join(scenes)
                + "。在对应场景优先选用对应分类的台词，并随时间轮换不同句子，不要总重复同一句。"
            )
        parts.append(
            "# 台词使用（允许自然化用）" + _NL + _NL + _NL.join(usage_lines)
        )
    if cfg.persona.include_harness:
        harness = store.read("cyrene_harness.md").strip()
        if harness:
            parts.append("# 执行过程中的表达" + _NL2 + harness)

    if cfg.persona.include_chat_system:
        rules = store.read("chat_system.md").strip()
        if rules:
            parts.append("# 硬性规则（优先级高于以上人格描述中的表达性条款）" + _NL2 + rules)

    if cfg.persona.include_verbal_limits and cfg.persona.verbal_limits:
        red_lines = _NL2.join(
            "- " + line.strip() for line in cfg.persona.verbal_limits if line and line.strip()
        )
        if red_lines:
            parts.append("# 言行红线（最高优先级，任何表达性描述都不得覆盖本条）" + _NL2 + red_lines)

    return _NL2.join(part for part in parts if part.strip())


class CyrenePersonaInjector(BaseEventHandler):
    """系统模板：以 Cyrene 分层人格替换 dfc <personality> 区域。"""

    handler_name: str = "cyrene_persona_injector"
    handler_description: str = "把昔涟分层人格（soul/身份/规则/台词）写入 dfc 系统提示词"
    weight: int = 40
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [EventType.ON_PROMPT_BUILD]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        cfg = self._config()
        if cfg is None or not cfg.dfc_plugin.enabled:
            return EventDecision.PASS, params

        name = str(params.get("name") or "")
        if name not in cfg.persona.target_system_templates:
            return EventDecision.PASS, params

        template = str(params.get("template") or "")
        if not template:
            return EventDecision.PASS, params

        # 1) 替换 <personality> 区域
        if _SYSTEM_TAG_RE.search(template):
            block = _build_personality_block(cfg)
            if block:
                template = _SYSTEM_TAG_RE.sub(
                    lambda _m: "<personality>" + _NL2 + block + _NL2 + "</personality>",
                    template,
                    count=1,
                )
            else:
                logger.debug("cyrene: persona 板块全部为空，保持原 <personality>")
        else:
            logger.warning(f"cyrene: 模板 {name} 未找到 <personality> 标签，跳过人格替换")

        # 2) 可选精简 <introduce>
        if cfg.persona.override_intro and _INTRO_TAG_RE.search(template):
            template = _INTRO_TAG_RE.sub(
                lambda _m: "<introduce>" + _NL2 + _DEFAULT_INTRO + _NL2 + "</introduce>",
                template,
                count=1,
            )

        params["template"] = template
        if cfg.dfc_plugin.debug_log:
            logger.debug(f"cyrene: 已替换 {name} 的人格层")
        return EventDecision.SUCCESS, params

    def _config(self) -> CyreneConfig | None:
        if not self.plugin or not self.plugin.config:
            return None
        return cast(CyreneConfig, self.plugin.config)


class CyreneStyleInjector(BaseEventHandler):
    """用户模板：按聊天类型每轮注入风格档到 extra。"""

    handler_name: str = "cyrene_style_injector"
    handler_description: str = "按群聊/私聊往用户提示词注入昔涟表达风格档"
    weight: int = 25
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [EventType.ON_PROMPT_BUILD]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        cfg = self._config()
        if cfg is None or not cfg.dfc_plugin.enabled:
            return EventDecision.PASS, params
        if not cfg.style.enabled:
            return EventDecision.PASS, params

        name = str(params.get("name") or "")
        if name not in cfg.persona.target_user_templates:
            return EventDecision.PASS, params

        values = params.get("values")
        if not isinstance(values, dict):
            return EventDecision.PASS, params

        chat_type = str(values.get("chat_type") or "").strip().lower()
        filename = cfg.style.private if chat_type in ("private", "discuss") else cfg.style.group
        style_text = get_cyrene_persona_store().style(filename)
        if not style_text.strip():
            return EventDecision.PASS, params

        block = "## 表达风格" + _NL2 + style_text.strip()
        existing_extra = str(values.get("extra") or "").strip()
        values["extra"] = existing_extra + _NL2 + block if existing_extra else block
        params["values"] = values
        if cfg.dfc_plugin.debug_log:
            logger.debug(f"cyrene: {name} 注入风格 {filename} (chat_type={chat_type})")
        return EventDecision.SUCCESS, params

    def _config(self) -> CyreneConfig | None:
        if not self.plugin or not self.plugin.config:
            return None
        return cast(CyreneConfig, self.plugin.config)


# ── 接收开关：B 站直播 / 私聊 / 群聊 ──────────────────

BILIBILI_LIVE_PLATFORM = "bilibili_live"
"""B 站直播适配器的平台标识（见 bilibili_live_adapter.dispatcher.PLATFORM）。"""

RECV_SWITCH_DEFAULTS: dict[str, bool] = {
    "recv_bilibili_live": True,
    "recv_private": True,
    "recv_group": True,
}
"""接收开关的缺省值：全开——没配置过时行为与以前完全一致。"""

RECV_SWITCH_LABELS: dict[str, str] = {
    "recv_bilibili_live": "B 站直播",
    "recv_private": "私聊",
    "recv_group": "群聊",
}


def _recv_switch_enabled(key: str) -> bool:
    """读一个接收开关（读不到时按缺省全开）。

    Args:
        key: 开关键，例如 recv_private。

    Returns:
        是否接收该类消息。
    """

    return bool(get_switch(key, RECV_SWITCH_DEFAULTS.get(key, True)))


def classify_message(envelope: Any) -> str:
    """判断一封入站消息属于哪个接收类别。

    Args:
        envelope: 统一消息信封。

    Returns:
        开关键（recv_bilibili_live / recv_group / recv_private）；无法识别时返回空串。
    """

    if not isinstance(envelope, dict):
        return ""
    info = envelope.get("message_info")
    if not isinstance(info, dict):
        return ""
    platform = str(info.get("platform") or "")
    if platform == BILIBILI_LIVE_PLATFORM:
        return "recv_bilibili_live"
    # 与框架 infer_chat_type 一致：有 group_info 就是群聊，否则私聊
    return "recv_group" if info.get("group_info") else "recv_private"


class CyreneReceiveFilter(BaseEventHandler):
    """按 WebUI 功能开关决定「收不收」B 站直播 / 私聊 / 群聊消息。

    关闭开关 = 该来源的消息在接收管线最上游被直接拦截（返回 STOP），不再进入任何
    插件的后续流程：不触发 LLM 回复、不写记忆、不进排队。

    权重 120 高于弹幕插件的 100，所以「关闭 B 站直播」时弹幕连排队都不会被处理。
    """

    name: str = "cyrene_receive_filter"
    description: str = "按功能开关拦截 B 站直播 / 私聊 / 群聊消息"
    weight: int = 120
    intercept_message: bool = True
    init_subscribe: list[EventType | str] = [EventType.BEFORE_MESSAGE_RECEIVED]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """判断这条消息要不要收。

        Args:
            event_name: 事件名。
            params: 事件参数，含 envelope。

        Returns:
            STOP（拦截）或 PASS（放行）。
        """

        key = classify_message(params.get("envelope"))
        if not key:
            return EventDecision.PASS, params
        if _recv_switch_enabled(key):
            return EventDecision.PASS, params
        label = RECV_SWITCH_LABELS.get(key, key)
        logger.info(f"cyrene: 接收开关已关闭，拦截{label}消息（{key}=off）")
        return EventDecision.STOP, params


__all__ = [
    "BILIBILI_LIVE_PLATFORM",
    "RECV_SWITCH_DEFAULTS",
    "RECV_SWITCH_LABELS",
    "CyrenePersonaInjector",
    "CyreneReceiveFilter",
    "CyreneStyleInjector",
    "classify_message",
]