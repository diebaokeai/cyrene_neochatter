"""按 QQ（私聊对象）分库：把原来的单人共享存储升级成每人一份。

数据布局::

    <base>/users/<user_id>/memory.json            分层记忆 / DFC 记忆
    <base>/users/<user_id>/proactive_state.json   主动行为状态
    <base>/users/_shared/memory.json              解析不到人时的兜底

设计要点：

- **解析失败不丢数据**：查不到 user_id（群聊、临时会话、框架异常）时落到 _shared，
  行为和改造前的「单一共享库」一致，绝不会因为解析失败把记忆写到别人的目录。
- **旧数据自动搬迁**：migrate_legacy_files 把根目录下的旧文件搬进 users/<owner>/，
  owner 取 memory.legacy_owner_id，留空则取 general.user_ids 的第一个。
- **缓存**：stream_id -> user_id 是稳定的一对一，进程内缓存，避免每轮查库。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Sequence

try:  # 插件内使用框架日志；单测环境下退化到标准库
    from src.app.plugin_system.api.log_api import get_logger

    logger = get_logger("cyrene_neochatter.user_scope")
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("cyrene_neochatter.user_scope")

SHARED_SCOPE = "_shared"
"""解析不到私聊对象时使用的目录名。"""

USERS_DIRNAME = "users"
"""用户分库的父目录名。"""

_user_id_cache: dict[str, str] = {}
"""stream_id -> user_id 缓存。"""


def clear_cache() -> None:
    """清空解析缓存（测试或人工切换身份时用）。"""

    _user_id_cache.clear()


async def resolve_user_id(stream_id: str) -> str:
    """通过 stream_id 解析私聊对象 user_id（带缓存）。

    Args:
        stream_id: 会话流 ID。

    Returns:
        QQ 号（或平台用户 ID）字符串；解析不到时返回空串。
    """

    sid = str(stream_id or "").strip()
    if not sid:
        return ""
    if sid in _user_id_cache:
        return _user_id_cache[sid]

    user_id = ""
    try:
        from src.core.managers.stream_manager import get_stream_manager
        from src.core.utils.user_query_helper import get_user_query_helper

        info = await get_stream_manager().get_stream_info(sid)
        person_id = (
            info.get("person_id")
            if isinstance(info, dict)
            else getattr(info, "person_id", None)
        )
        if person_id:
            person = await get_user_query_helper().person_crud.get_by(person_id=str(person_id))
            if person:
                user_id = str(getattr(person, "user_id", "") or "")
    except Exception as exc:
        logger.debug(f"user_scope 解析私聊对象失败（按共享库处理）: {exc}")
        user_id = ""

    _user_id_cache[sid] = user_id
    return user_id


async def resolve_user_id_from_values(values: dict[str, Any] | None) -> str:
    """从事件参数（提示词构建 values）里解析私聊对象 user_id。

    Args:
        values: 事件参数，通常含 stream_id。

    Returns:
        QQ 号字符串；解析不到时返回空串。
    """

    if not isinstance(values, dict):
        return ""
    return await resolve_user_id(str(values.get("stream_id") or ""))


def cached_user_id(stream_id: str) -> str:
    """同步读取已缓存的 stream -> user_id（没解析过时返回空串）。

    给同步代码用（例如主动状态的落盘）；空串会落到 _shared，不会写错人。
    """

    return _user_id_cache.get(str(stream_id or "").strip(), "")


def legacy_owner(cfg: Any) -> str:
    """旧版单人数据的归属人。

    Args:
        cfg: CyreneNeoConfig（鸭子类型，只读 memory.legacy_owner_id 与 general.user_ids）。

    Returns:
        user_id；两边都没配时返回空串。
    """

    memory = getattr(cfg, "memory", None)
    explicit = str(getattr(memory, "legacy_owner_id", "") or "").strip()
    if explicit:
        return explicit
    general = getattr(cfg, "general", None)
    for item in getattr(general, "user_ids", None) or []:
        text = str(item or "").strip()
        if text:
            return text
    return ""


def scope_key(user_id: str | None) -> str:
    """把 user_id 规整成目录名（空 -> _shared）。"""

    text = str(user_id or "").strip()
    if not text:
        return SHARED_SCOPE
    # 目录名安全化：QQ 号本身没问题，但防御平台 ID 里混进路径分隔符
    safe = text.replace("/", "_").replace("\\", "_").replace("..", "_")
    return safe or SHARED_SCOPE


def users_root(base: str) -> Path:
    """返回用户分库的根目录。"""

    return Path(base or ".") / USERS_DIRNAME


def scope_dir(base: str, user_id: str | None) -> str:
    """返回某个人的数据目录（不创建）。

    Args:
        base: 插件配置里的公共目录，例如 data/cyrene_neochatter。
        user_id: 私聊对象 ID；空值 = 共享兜底目录。

    Returns:
        形如 data/cyrene_neochatter/users/3308747519 的目录字符串。
    """

    return str(users_root(base) / scope_key(user_id))


def is_legacy_layout(base: str) -> bool:
    """判断是否还是改造前的单人布局（根目录直接有数据）。"""

    root = Path(base or ".")
    return root.is_dir() and any((root / name).is_file() for name in ("memory.json",))


def list_user_dirs(base: str) -> list[str]:
    """列出该 base 下已经存在的用户目录名（不含 _shared），按名字排序。

    Args:
        base: 公共目录。

    Returns:
        目录名列表（即 user_id 列表）。
    """

    root = users_root(base)
    if not root.is_dir():
        return []
    names = [
        item.name
        for item in root.iterdir()
        if item.is_dir() and item.name != SHARED_SCOPE and not item.name.startswith(".")
    ]
    return sorted(names)


def user_label(base: str, user_id: str) -> str:
    """给面板用的友好标签：优先取分层记忆里的 l0 昵称前 14 字。

    Args:
        base: 分层记忆公共目录。
        user_id: 用户 ID。

    Returns:
        标签字符串（可能为空）。
    """

    import json

    path = Path(scope_dir(base, user_id)) / "memory.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    l0 = data.get("l0")
    if isinstance(l0, dict):
        for key in ("nickname", "preferredName", "permanentNote"):
            text = str(l0.get(key) or "").strip()
            if text:
                return text[:14]
    return ""


def migrate_legacy_files(
    base: str,
    *,
    owner_id: str,
    names: Sequence[str] = ("memory.json",),
) -> dict[str, str]:
    """把旧版（单人布局）根目录下的文件搬进 owner 的用户目录。

    幂等：目标已存在时不覆盖、也不删源文件（留给人工处理）。

    Args:
        base: 公共目录。
        owner_id: 归属用户 ID（旧数据的主人）。
        names: 需要搬迁的文件名列表。

    Returns:
        搬迁结果：文件名 -> 新路径。
    """

    root = Path(base or ".")
    target_dir = root / USERS_DIRNAME / scope_key(owner_id)
    moved: dict[str, str] = {}
    for name in names:
        source = root / name
        if not source.is_file():
            continue
        target = target_dir / name
        if target.exists():
            logger.warning(f"cyrene: 旧文件 {source} 与目标 {target} 同时存在，跳过搬迁（请人工确认）")
            continue
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
        except Exception as exc:
            logger.warning(f"cyrene: 旧文件搬迁失败 {source} -> {target}: {exc}")
            continue
        moved[name] = str(target)
        logger.info(f"cyrene: 旧数据已按人分库 {source} -> {target}")
    return moved


__all__ = [
    "SHARED_SCOPE",
    "USERS_DIRNAME",
    "cached_user_id",
    "clear_cache",
    "is_legacy_layout",
    "legacy_owner",
    "list_user_dirs",
    "migrate_legacy_files",
    "resolve_user_id",
    "resolve_user_id_from_values",
    "scope_dir",
    "scope_key",
    "user_label",
    "users_root",
]
