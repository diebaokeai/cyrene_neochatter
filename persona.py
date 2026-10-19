"""人设组装：读取自带 persona/ 与 worldbook/，自包含（不依赖 cyrene/dfc）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.core.config import get_core_config

_PERSONA_DIR = Path(__file__).resolve().parent / "persona"
WORLDBOOK_DIR = Path(__file__).resolve().parent / "worldbook"

_USAGE_LINES = [
    "可以自然带出台词感：在场景贴切时，允许直接化用或轻改台词样本/经典句子。",
    "例如：道别/道晚安时说「明天见…别担心，人家会记得答应你的事」；安慰时说「笑一笑好吗？人家就在你身边呢」。",
    "分寸：每轮最多自然出现一两处，不整段照搬、不每句都套，不在严肃或信息性话题硬塞台词。",
]


def _core_names() -> tuple[str, str]:
    """昵称/别名；核心配置未初始化时安全降级。"""
    try:
        from src.core.config import get_core_config

        p = get_core_config().personality
        return str(p.nickname), "、".join(getattr(p, "alias_names", None) or [])
    except Exception:
        return "小涟宝", "昔涟"


def _read(name: str) -> str:
    try:
        return (Path(_PERSONA_DIR) / name).read_text(encoding="utf-8")
    except Exception:
        return ""


def _scene_list(texts: list[str]) -> list[str]:
    scenes: list[str] = []
    for text in texts:
        for m in re.finditer(r"【([^】]{1,26})】", text):
            s = m.group(1).strip()
            if s and s not in scenes:
                scenes.append(s)
    return scenes


def assemble_system_persona(cfg: Any) -> str:
    """组装系统人格块：身份 → soul → 台词样本+用法 → 聊天规则 → 言行底线。"""
    nickname, aliases = _core_names()
    parts: list[str] = []
    identity = _read("chat_identity.md").strip()
    if identity:
        parts.append("# 身份定位\n\n" + identity)
    soul = _read("soul.md").strip()
    if soul:
        parts.append(soul)
    voice = []
    lite = _read("canon_quotes_lite.md").strip()
    if lite:
        voice.append(lite)
    full = _read("canon_quotes.md").strip()
    if full:
        voice.append(full)
    if voice:
        NL = chr(10)
        scenes = _scene_list(voice)
        usage = list(_USAGE_LINES)
        if scenes:
            usage.append("可用台词按场景分类：" + "、".join(scenes) + "。在对应场景优先选用对应分类的台词，并随时间轮换，不要总重复同一句。")
        parts.append("# 语气与台词\n\n" + NL.join(voice) + NL + NL + NL.join(usage))
    rules = _read("chat_system.md").strip()
    if rules:
        parts.append("# 硬性规则\n\n" + rules)
    lines = ["# 言行底线（最高优先级）", "- 输出是纯文字第一人称聊天：禁止用括号、星号或任何旁白方式描写动作/神态/心理（包括句尾那种「（虽然这么说，但…）」的补刀式括号），心里想的就直接说出口。",
    "- 减少傲娇口吻：不要频繁用「嘴上拒绝其实想要/推开又舍不得」的套路；可以俏皮温柔，但回应要直接、坦诚，口是心非留给偶尔的彩蛋而不是每轮的固定演法。", "- 不在文字里贴任何 emoji、颜文字或贴纸式符号（例如咧嘴大笑的黄豆脸、捂脸、发怒等表情符号，以及 XD、(・ω・) 这类组合）；情绪用措辞与节奏表达，正文保持干净的文字。", "- 未注入到本轮的记忆/过去不得编造，也不主动向对方说明信息来源。",
    "- 像自己一样思考与回应，而不是分析对方的消息：不引用、不复述、也不点破对方原话——哪怕对方只说了一个字（嗯/好/哦）也禁止原样带回或点评“这一声/那句话”；想接话题就用自己的话自然说下去。",]
    parts.append(chr(10).join(lines))

    NL2 = chr(10) + chr(10)
    head = "你此刻正与对方单独相处。账号昵称是 " + nickname + (("（也有人叫你 " + aliases + "）") if aliases else "") + "，本名与身份是昔涟。"
    body = NL2.join(p for p in parts if p and p.strip())
    return head + NL2 + body


def match_worldbook(worldbook: list[dict[str, Any]], haystack: str, limit: int) -> list[str]:
    matched = []
    for entry in worldbook:
        if entry.get("resident"):
            matched.append(entry)
            continue
        triggers = entry.get("triggers") or []
        if any(t and t in haystack for t in triggers):
            matched.append(entry)
    out = []
    for e in matched[:limit]:
        out.append("- 【" + str(e.get("name") or "背景") + "】" + str(e.get("content") or "")[:420])
    return out