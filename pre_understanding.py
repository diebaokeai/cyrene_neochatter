"""回复前轻量上下文理解（CITA 风格：指代消解 / 查询改写 / 焦点提取）。"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[: max(1, int(limit))]


async def _send_and_collect(request: Any, timeout: float) -> Any:
    response = await asyncio.wait_for(request.send(stream=False), timeout=timeout)
    await asyncio.wait_for(response, timeout=timeout)
    return response


def _parse_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            import json_repair

            parsed = json_repair.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


async def understand_turn(
    infer: Any,
    cfg: Any,
    history_lines: list[str],
    user_text: str,
) -> dict[str, Any]:
    """返回 {resolved_query, focus, refs}；关闭/失败/超时统一返回空 dict。"""
    section = getattr(cfg, "understanding", None)
    if section is None or not bool(getattr(section, "enabled", True)):
        return {}
    task = str(getattr(section, "task", "utils_small") or "utils_small")
    max_history = int(getattr(section, "max_history_chars", 1200) or 0)
    max_chars = int(getattr(section, "max_chars", 200) or 200)
    timeout = float(getattr(section, "timeout_seconds", 8.0) or 8.0)
    history = chr(10).join(history_lines)
    if max_history > 0:
        history = history[-max_history:]
    try:
        from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
        from src.kernel.llm import LLMPayload, ROLE, Text

        request = create_llm_request(
            get_model_set_by_task(task),
            request_name="cyrene_neochatter_understanding",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(
            "你是对话上下文理解服务。只做三件事："
            "1) 指代消解：他/她/那个/刚才那事分别指谁或什么；"
            "2) 查询改写：把省略、碎片化的最新发言补全成完整意图；"
            "3) 焦点提取：这段对话当前真正在谈什么。"
            "严格输出一个 JSON：{\"resolved_query\": \"\", \"focus\": \"\", \"refs\": {}}。"
            "无法判断的字段给空字符串或空对象；不要输出多余文字。"
        )))
        request.add_payload(LLMPayload(ROLE.USER, Text(
            "【最近的对话】" + chr(10) + history + chr(10) + chr(10) + "【最新发言】" + chr(10) + user_text
        )))
        response = await _send_and_collect(request, timeout)
        parsed = _parse_json(str(getattr(response, "message", "") or ""))
        if not parsed:
            return {}
        refs = parsed.get("refs")
        info = {
            "resolved_query": _clip(parsed.get("resolved_query"), max_chars),
            "focus": _clip(parsed.get("focus"), max_chars),
            "refs": refs if isinstance(refs, dict) else {},
        }
        if not (info["resolved_query"] or info["focus"] or info["refs"]):
            return {}
        return info
    except Exception:
        return {}


def format_understanding(info: dict[str, Any]) -> str:
    """把理解结果格式化成注入块（空则返回空串）。"""
    if not info:
        return ""
    parts: list[str] = []
    resolved = str(info.get("resolved_query") or "").strip()
    focus = str(info.get("focus") or "").strip()
    refs = info.get("refs") if isinstance(info.get("refs"), dict) else {}
    if resolved:
        parts.append("对方这轮真正想说的：" + resolved)
    if focus:
        parts.append("当前焦点：" + focus)
    if refs:
        pairs = [f"{k}→{v}" for k, v in list(refs.items())[:6] if str(v or "").strip()]
        if pairs:
            parts.append("指代：" + "；".join(pairs))
    if not parts:
        return ""
    return "【本轮理解】" + chr(10) + chr(10).join(parts)
