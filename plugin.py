"""cyrene_neochatter 插件入口（自包含私聊引擎；默认关闭）。"""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.text_policy import strip_emoji as _strip_emoji, strip_tool_markup as _strip_tool_markup
from src.core.components import BasePlugin, register_plugin

from .config import CyreneNeoConfig
from .llm import Inference
from . import user_scope
from .memory import default_source_dir, load_store, migrate
from .persona import WORLDBOOK_DIR, assemble_system_persona
from .proactive import ProactiveState, should_trigger

from .dfc_memory import memory_store as dfc_memory_store
from .dfc_memory.injector import CyreneMemoryInjector
from .dfc_memory.writer import CyreneMemoryWriter
from .event_handler import (
    CyrenePersonaInjector,
    CyreneReceiveFilter,
    CyreneStyleInjector,
    _build_personality_block,
)

logger = get_logger("cyrene_neochatter")


@register_plugin
class CyreneNeoPlugin(BasePlugin):
    """昔涟私聊引擎（F 桌样式；独立于 dfc/cyrene/nfc）。"""

    plugin_name: str = "cyrene_neochatter"
    plugin_description: str = "昔涟私聊引擎：分层记忆+心理活动流+主动想起（以 F:\昔涟agent 为底）"
    plugin_version: str = "0.1.0"

    configs: list[type] = [CyreneNeoConfig]
    dependent_components: list[str] = []

    def __init__(self, config: CyreneNeoConfig | None = None) -> None:
        super().__init__(config)
        self._proactive_task_id: str | None = None
        self._webui_task_id: str | None = None
        self._cnc_turn_count = 0
        self._cnc_streams: set[str] = set()
        self._cnc_proactive: dict[str, dict[str, float | str]] = {}
        self._cnc_meme_last: dict[str, float] = {}

    def _state_name(self, cfg: CyreneNeoConfig) -> str:
        return str(
            getattr(cfg.proactive, "state_file", "proactive_state.json")
            or "proactive_state.json"
        )

    def _state_paths(self, cfg: CyreneNeoConfig) -> list[Path]:
        """所有可能的主动状态文件：旧版根目录文件 + 每个用户目录的文件。"""

        root_dir = cfg.memory.data_dir or "data/cyrene_neochatter"
        name = self._state_name(cfg)
        paths = [Path(root_dir) / name]
        users_root = user_scope.users_root(root_dir)
        if users_root.is_dir():
            for child in sorted(users_root.iterdir()):
                if child.is_dir():
                    paths.append(child / name)
        return paths

    async def _load_proactive_state(self, cfg: CyreneNeoConfig) -> None:
        """从磁盘恢复主动行为状态（重启不丢；兼容旧版单人文件）。"""

        import json

        for path in self._state_paths(cfg):
            try:
                if not path.is_file():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for stream_id, item in data.items():
                        if isinstance(item, dict):
                            self._cnc_proactive.setdefault(str(stream_id), item)
                            self._cnc_streams.add(str(stream_id))
            except Exception as exc:
                logger.warning(f"主动状态读取失败（忽略）{path}: {exc}")
        # 预热 stream -> user_id 缓存：这样首次落盘就能按人分文件，不会误落 _shared
        for stream_id in list(self._cnc_proactive):
            await user_scope.resolve_user_id(stream_id)
        logger.info(f"cyrene_neochatter 主动状态已恢复: {len(self._cnc_proactive)} 个流")

    def _write_proactive_groups(
        self, cfg: CyreneNeoConfig, groups: dict[str, dict[str, Any]]
    ) -> None:
        """把分组好的状态写到各自的用户目录。"""

        import json

        root_dir = cfg.memory.data_dir or "data/cyrene_neochatter"
        name = self._state_name(cfg)
        for key, payload in groups.items():
            path = user_scope.users_root(root_dir) / key / name
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
                )
            except Exception as exc:
                logger.debug(f"主动状态落盘失败 {path}: {exc}")

    def _save_proactive_state(self, cfg: CyreneNeoConfig) -> None:
        """按人分库落盘主动状态（同步版，用已缓存的 stream -> user_id）。

        缓存里还没有映射的流**跳过不写**（留给 _save_proactive_state_async 补），
        避免某个人的状态被误写进 _shared。
        """

        groups: dict[str, dict[str, Any]] = {}
        for stream_id, item in self._cnc_proactive.items():
            user_id = user_scope.cached_user_id(stream_id)
            if not user_id:
                continue
            groups.setdefault(user_scope.scope_key(user_id), {})[stream_id] = item
        self._write_proactive_groups(cfg, groups)

    async def _save_proactive_state_async(self, cfg: CyreneNeoConfig) -> None:
        """异步版：逐个解析 stream -> user_id，只有真的解析不到才落 _shared。"""

        groups: dict[str, dict[str, Any]] = {}
        for stream_id, item in self._cnc_proactive.items():
            user_id = await user_scope.resolve_user_id(stream_id)
            groups.setdefault(user_scope.scope_key(user_id), {})[stream_id] = item
        self._write_proactive_groups(cfg, groups)

    def _note_stream_activity(self, stream_id: str, now: float | None = None) -> None:
        """记录对方活跃：刷新沉默时间、清零未回应、亲密度小幅上升、欲望回落。"""
        self._cnc_streams.add(stream_id)
        ts = float(now) if now is not None else time.time()
        st = self._cnc_proactive.setdefault(
            stream_id,
            {
                "last_message_ts": ts,
                "last_proactive_ts": 0.0,
                "scheduled_at": 0.0,
                "scheduled_reason": "",
                "paused": 0,
                "pause_reason": "",
                "unanswered_count": 0,
                "desire": 0.0,
                "affinity": 0.0,
                "last_scene": "",
                "epoch": 0,
            },
        )
        st["last_message_ts"] = ts
        st["scheduled_at"] = 0.0
        st["scheduled_reason"] = ""
        st["unanswered_count"] = 0
        st["desire"] = max(0.0, float(st.get("desire") or 0.0) - 0.3)
        st["affinity"] = min(1.0, float(st.get("affinity") or 0.0) + 0.02)
        try:
            cfg = self.config if isinstance(self.config, CyreneNeoConfig) else CyreneNeoConfig()
            self._save_proactive_state(cfg)
        except Exception:
            pass

    def get_components(self) -> list[type]:
        cfg = self.config if isinstance(self.config, CyreneNeoConfig) else CyreneNeoConfig()
        if not cfg.general.enabled:
            return []
        from .chatter import CyreneNeoChatter
        from .router import NeoChatterMemoryAdminRouter

        components: list[type] = [
            # 接收开关（B 站直播 / 私聊 / 群聊）必须先注册：它是消息管线最上游的闸门
            CyreneReceiveFilter,
            CyreneNeoChatter,
            NeoChatterMemoryAdminRouter,
        ]
        if cfg.dfc_plugin.enabled:
            components.extend([
                CyrenePersonaInjector,
                CyreneStyleInjector,
                CyreneMemoryInjector,
                CyreneMemoryWriter,
            ])
        return components

    async def on_plugin_loaded(self) -> None:
        cfg = self.config if isinstance(self.config, CyreneNeoConfig) else CyreneNeoConfig()
        if not cfg.general.enabled:
            logger.info("cyrene_neochatter 默认关闭，跳过初始化")
            return
        # 旧版单人布局 -> 按 QQ 分库（幂等；owner = memory.legacy_owner_id 或名单第一个人）
        strat_root = cfg.memory.data_dir or "data/cyrene_neochatter"
        dfc_root = cfg.memory_store.data_dir or "data/cyrene_memory"
        owner = user_scope.legacy_owner(cfg)
        if owner:
            moved = user_scope.migrate_legacy_files(
                strat_root, owner_id=owner, names=("memory.json", "proactive_state.json")
            )
            moved.update(
                user_scope.migrate_legacy_files(dfc_root, owner_id=owner, names=("memory.json",))
            )
            if moved:
                logger.info(f"cyrene_neochatter 旧记忆已按人分库 owner={owner}: {sorted(moved)}")
        else:
            logger.warning("cyrene_neochatter 旧记忆未分库：general.user_ids 与 memory.legacy_owner_id 都为空")
        strat_base = user_scope.scope_dir(strat_root, owner)
        dfc_base = user_scope.scope_dir(dfc_root, owner)
        # 迁移 F 桌记忆（幂等）
        try:
            if cfg.memory.enabled and cfg.memory.auto_migrate:
                source = (cfg.memory.source_dir or "").strip() or default_source_dir()
                if Path(source).is_dir():
                    summary = migrate(source, strat_base, str(WORLDBOOK_DIR))
                    logger.info(f"cyrene_neochatter 迁移结果: {summary}")
        except Exception as exc:
            logger.warning(f"cyrene_neochatter 迁移失败: {exc}")
        # DFC 人格层的记忆迁移（并入自 cyrene 插件）
        try:
            if cfg.dfc_plugin.enabled and cfg.dfc_memory.enabled and cfg.memory_store.auto_migrate:
                data = dfc_memory_store.load_memory(dfc_base)
                if not (data.get("meta", {}).get("migrated") and data.get("worldbook")):
                    source = (cfg.memory_store.source_dir or "").strip() or dfc_memory_store.default_source_dir()
                    wb = (cfg.memory_store.worldbook_dir or "").strip() or None
                    if source and Path(source).is_dir():
                        summary = dfc_memory_store.migrate(source, dfc_base, worldbook_dir=wb)
                        logger.info(f"cyrene_neochatter(dfc) 记忆迁移结果: {summary}")
                        if bool(getattr(cfg.memory_inject, "nfc_system_override", False)):
                            from .dfc_memory.nfc_override import ensure_nfc_override_file

                            # NFC override 是外部工具按固定路径读的，仍放公共目录
                            out_path = ensure_nfc_override_file(dfc_root, _build_personality_block(cfg))
                            if out_path:
                                logger.info(f"cyrene_neochatter(dfc) 已生成 NFC override: {out_path}")
        except Exception as exc:
            logger.warning(f"cyrene_neochatter(dfc) 记忆迁移失败: {exc}")
        # 主动想起轮询（勿扰/概率/预约兜底）
        if cfg.proactive.enabled:
            await self._load_proactive_state(cfg)
            self._start_proactive_loop(cfg)
        # 启动后自动打开 Cyrene WebUI（默认关闭，config [webui] auto_open）
        if bool(getattr(cfg.webui, "auto_open", False)):
            self._start_webui_autostart(cfg)

    def _start_webui_autostart(self, cfg: CyreneNeoConfig) -> None:
        """起一个后台任务，等 HTTP 服务就绪后用默认浏览器打开 Cyrene WebUI。"""
        from src.kernel.concurrency import get_task_manager

        info = get_task_manager().create_task(
            self._auto_open_webui(cfg),
            name="cyrene_neochatter_webui_autostart",
            daemon=True,
            metadata={"plugin": self.plugin_name, "purpose": "webui_autostart"},
        )
        self._webui_task_id = getattr(info, "task_id", None)

    @staticmethod
    def _webui_host_port() -> tuple[str, int]:
        """取 http_router 的 host/port：优先运行时 server，其次 core.toml 配置。"""
        host = ""
        port = 0
        try:
            from src.core.transport.router.http_server import get_http_server

            server = get_http_server()
            if server is not None:
                host = str(getattr(server, "host", "") or "")
                port = int(getattr(server, "port", 0) or 0)
        except Exception as exc:
            logger.debug(f"读取运行时 HTTP 服务信息失败: {exc}")
        if not host or not port:
            try:
                from src.core.config import get_core_config

                section = get_core_config().http_router
                host = host or str(getattr(section, "http_router_host", "") or "")
                port = port or int(getattr(section, "http_router_port", 0) or 0)
            except Exception as exc:
                logger.debug(f"读取 core.toml http_router 配置失败: {exc}")
        if not host or not port:
            # 最后兜底：core config 尚未初始化时直接解析 config/core.toml
            try:
                import tomllib

                data = tomllib.loads(Path("config/core.toml").read_text(encoding="utf-8"))
                section = data.get("http_router") or {}
                host = host or str(section.get("http_router_host") or "")
                port = port or int(section.get("http_router_port") or 0)
            except Exception as exc:
                logger.debug(f"直接解析 config/core.toml 失败: {exc}")
        if host in {"0.0.0.0", "::", "localhost", ""}:
            host = "127.0.0.1"
        return host, port

    async def _auto_open_webui(self, cfg: CyreneNeoConfig) -> None:
        """等 HTTP 服务就绪后用默认浏览器打开控制台。

        - 配置了 webui.auto_open_url 就直接用它（可指向主 WebUI /webui/frontend）；
        - 否则用 http_router 的 host/port（运行时 server 优先，core.toml 兜底）
          拼出 /cyrene-webui/；
        - 轮询等 HTTP 服务真正起来（最多 60 秒），到点仍未就绪也会尝试打开。
        """
        import webbrowser

        delay = max(0.0, float(getattr(cfg.webui, "auto_open_delay", 3.0) or 0.0))
        await asyncio.sleep(delay)
        url = str(getattr(cfg.webui, "auto_open_url", "") or "").strip()
        if not url:
            host, port = self._webui_host_port()
            if host and port:
                url = f"http://{host}:{port}/cyrene-webui/"
        if not url:
            logger.warning("Cyrene WebUI 自动打开失败：拿不到 http_router 的 host/port")
            return
        deadline = time.time() + 60.0
        ready = False
        while time.time() < deadline:
            try:
                from src.core.transport.router.http_server import get_http_server

                server = get_http_server()
                if server is not None and server.is_running():
                    ready = True
                    break
            except Exception as exc:
                logger.debug(f"探测 HTTP 服务失败（继续重试）: {exc}")
            await asyncio.sleep(1.0)
        if not ready:
            logger.warning(f"HTTP 服务 60 秒内未就绪，仍然尝试打开: {url}")
        try:
            opened = webbrowser.open(url)
            if opened:
                logger.info(f"Cyrene WebUI 已自动打开: {url}")
            else:
                logger.warning(f"Cyrene WebUI 自动打开未成功（可能没有可用浏览器）: {url}")
        except Exception as exc:
            logger.warning(f"Cyrene WebUI 自动打开异常: {exc}")

    async def on_plugin_unloaded(self) -> None:
        if self._proactive_task_id is not None:
            try:
                from src.kernel.concurrency import get_task_manager

                await get_task_manager().cancel_task(self._proactive_task_id)
            except Exception:
                pass
            self._proactive_task_id = None
        if self._webui_task_id is not None:
            try:
                from src.kernel.concurrency import get_task_manager

                await get_task_manager().cancel_task(self._webui_task_id)
            except Exception:
                pass
            self._webui_task_id = None

    def _start_proactive_loop(self, cfg: CyreneNeoConfig) -> None:
        from src.kernel.concurrency import get_task_manager

        info = get_task_manager().create_task(
            self._proactive_loop(cfg),
            name="cyrene_neochatter_proactive",
            daemon=True,
            metadata={"plugin": self.plugin_name, "purpose": "proactive"},
        )
        self._proactive_task_id = getattr(info, "task_id", None)

    async def _proactive_loop(self, cfg: CyreneNeoConfig) -> None:
        interval = max(5, int(cfg.proactive.check_interval))
        while True:
            try:
                await self._proactive_once(cfg)
            except Exception as exc:
                logger.debug(f"主动想起检查异常: {exc}")
            await asyncio.sleep(interval)

    @staticmethod
    def _proactive_on(key: str, default: bool) -> bool:
        """主动想起的运行时开关（Cyrene WebUI - 功能开关，热加载、重启保留）。"""
        try:
            from src.app.plugin_system.api.feature_switch_api import get_switch

            return bool(get_switch(key, bool(default)))
        except Exception:
            return bool(default)

    async def _proactive_recent_lines(self, cfg: CyreneNeoConfig, stream_id: str) -> list[str]:
        """取该 stream 的最近对话，格式化成「你：…」/「对方（名字）：…」。"""
        limit = int(getattr(cfg.proactive, "context_limit", 12) or 0)
        if limit <= 0:
            return []
        try:
            from src.app.plugin_system.api.stream_api import get_stream_messages

            from .chatter import _fmt_history

            msgs = await get_stream_messages(stream_id=stream_id, limit=max(limit, 5), offset=0)
        except Exception as exc:
            logger.debug(f"主动想起读取最近对话失败: {exc}")
            return []
        lines = [line for line in (_fmt_history(m) for m in (msgs or [])) if line]
        return lines[-limit:]

    async def _proactive_context(self, cfg: CyreneNeoConfig, stream_id: str, reason: str) -> str:
        """构建主动想起要注入的上下文与记忆块；两者各有运行时功能开关。"""
        NL = chr(10)
        blocks: list[str] = []
        recent: list[str] = []
        want_context = self._proactive_on(
            "cyrene_proactive_context", bool(getattr(cfg.proactive, "inject_context", True))
        )
        want_memory = self._proactive_on(
            "cyrene_proactive_memory", bool(getattr(cfg.proactive, "inject_memory", True))
        )
        if want_context:
            recent = await self._proactive_recent_lines(cfg, stream_id)
            if recent:
                blocks.append(
                    "## 最近的对话（按时间顺序，仅供理解语境；不要复述或引用原话）" + NL + NL.join(recent)
                )
        if want_memory and bool(getattr(cfg.memory, "enabled", True)):
            try:
                from .chatter import CyreneNeoChatter

                helper = CyreneNeoChatter(stream_id, self)
                data = await helper._ensure_memory()
                haystack = " ".join([reason, *recent[-4:]])[:600]
                extra = await helper._build_memory_extra(data, haystack)
                if extra and extra.strip():
                    blocks.append(extra.strip())
            except Exception as exc:
                logger.debug(f"主动想起注入记忆失败: {exc}")
        if not blocks:
            return ""
        return chr(10) + chr(10) + NL.join(blocks)

    async def _proactive_once(self, cfg: CyreneNeoConfig) -> None:
        if not self._cnc_streams:
            return
        now = time.time()
        for stream_id in list(self._cnc_streams):
            st = self._cnc_proactive.setdefault(
                stream_id,
                {"last_message_ts": now, "last_proactive_ts": 0.0, "scheduled_at": 0.0, "scheduled_reason": "", "paused": 0, "pause_reason": "", "unanswered_count": 0, "desire": 0.0, "affinity": 0.0, "last_scene": "", "epoch": 0},
            )
            silence = now - float(st.get("last_message_ts") or now)
            if silence >= int(cfg.proactive.silence_threshold):
                # 沉默越久，想联系的欲望缓慢累积（每次检查 +0.005）
                st["desire"] = min(1.0, float(st.get("desire") or 0.0) + 0.005)
            state = ProactiveState(
                stream_id=stream_id,
                last_message_ts=float(st.get("last_message_ts") or now),
                last_proactive_ts=float(st.get("last_proactive_ts") or 0.0),
                scheduled_at=float(st.get("scheduled_at") or 0.0),
                scheduled_reason=str(st.get("scheduled_reason") or ""),
                paused=bool(st.get("paused")),
                pause_reason=str(st.get("pause_reason") or ""),
                unanswered_count=int(st.get("unanswered_count") or 0),
                desire=float(st.get("desire") or 0.0),
                affinity=float(st.get("affinity") or 0.0),
                last_scene=str(st.get("last_scene") or ""),
                epoch=int(st.get("epoch") or 0),
            )
            if not should_trigger(
                state,
                now=now,
                silence_threshold=int(cfg.proactive.silence_threshold),
                trigger_probability=float(cfg.proactive.trigger_probability),
                min_interval=int(cfg.proactive.min_interval),
                quiet_start=cfg.proactive.quiet_start,
                quiet_end=cfg.proactive.quiet_end,
                unanswered_boost=float(getattr(cfg.proactive, "unanswered_boost", 0.0) or 0.0),
            ):
                continue
            reason = state.scheduled_reason or "突然想起了对方"
            try:
                infer = Inference(cfg, self)
                from .chatter import current_time_text

                extras = await self._proactive_context(cfg, stream_id, reason)
                system = (
                    assemble_system_persona(cfg)
                    + extras
                    + chr(10) + chr(10)
                    + "你正想主动联系对方：只依据上面真实发生过的事说话，不要编造近期对话里没有的内容。"
                )
                user_msg = (
                    "现在是主动想起的时刻（当前时间：" + current_time_text() + "）。"
                    "理由：" + reason + "。"
                    "像平时聊天一样发一两句简短、自然、贴合关系的话（可以顺带提一句最近聊过或答应过的事，但别复述原话）。"
                )
                line = _strip_emoji(_strip_tool_markup(await infer.chat(system, user_msg)))
                sent_ok = False
                if line and line.strip():
                    from src.app.plugin_system.api.send_api import send_text

                    sent_ok = bool(await send_text(content=line.strip()[:300], stream_id=stream_id))
                    if sent_ok:
                        logger.info(f"cyrene_neochatter 主动想起已发送 stream={stream_id}")
            except Exception as exc:
                logger.warning(f"主动想起发送失败: {exc}")
                sent_ok = False
            st["last_proactive_ts"] = now
            st["scheduled_at"] = 0.0
            st["scheduled_reason"] = ""
            st["last_scene"] = reason
            st["epoch"] = int(st.get("epoch") or 0) + 1
            if sent_ok:
                st["unanswered_count"] = int(st.get("unanswered_count") or 0) + 1
                st["desire"] = max(0.0, float(st.get("desire") or 0.0) - 0.3)
            await self._save_proactive_state_async(cfg)