"""NFC system_prompt_override 生成器（可选，默认关闭）。

当 config.memory_inject.nfc_system_override=true 且已安装 neo_fatum_chatter 时，
读取 NFC 默认系统模板，用 cyrene 人格（soul/canon/红线）替换 <personality> 内容，
生成建议文件 data/cyrene_memory/nfc_system_prompt_override.txt，再由你贴入 NFC 配置
prompt.system_prompt_override。不自动改写 NFC 配置，避免损坏其标签校验。
"""

from __future__ import annotations

import re
from pathlib import Path

_PERSONALITY_RE = re.compile(r"<personality>.*?</personality>", re.S | re.I)


def _sanitize_placeholders(body: str) -> str:
    """把人格正文里的自定义占位符清洗为纯文本，避免触发 NFC 校验与渲染。"""
    body = body.replace("{{user_nickname}}", "对方昵称").replace("{user_nickname}", "对方昵称")
    body = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", r"\1", body)
    return body


def build_nfc_override_text(persona_block: str) -> str | None:
    """返回 NFC 覆盖系统提示词文本；NFC 未安装或取不到模板时返回 None。"""
    try:
        from neo_fatum_chatter.prompts.templates import NFC_SYSTEM_PROMPT
    except Exception:
        return None
    template = NFC_SYSTEM_PROMPT
    if _PERSONALITY_RE.search(template):
        body = _sanitize_placeholders(persona_block.strip())
        return _PERSONALITY_RE.sub(lambda _m: '<personality>' + "\n" + body + "\n" + '</personality>', template, count=1)
    return None


def ensure_nfc_override_file(base: str, persona_block: str) -> str | None:
    text = build_nfc_override_text(persona_block)
    if not text:
        return None
    out = Path(base) / 'nfc_system_prompt_override.txt'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding='utf-8')
    return str(out)


__all__ = ["build_nfc_override_text", "ensure_nfc_override_file"]