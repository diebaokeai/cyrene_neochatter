"""cyrene_neochatter 记忆管理后台 Router（风格沿用 booku/cyrene 记忆后台）。"""

from __future__ import annotations

import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from src.app.plugin_system.api import log_api
from src.app.plugin_system.api.feature_switch_api import get_switch, set_switch
from src.core.components import BaseRouter
from src.core.components.base.config import SectionBase

from .. import user_scope
from ..config import CyreneNeoConfig
from ..dfc_memory import memory_store as dfc_memory_store
from ..memory import load_store, save_store

logger = log_api.get_logger("cyrene_neochatter.memory_admin")

_CURRENT_USER: ContextVar[str] = ContextVar("cyrene_admin_user", default="")
"""当前面板请求要看的私聊对象（由 ?user=<qq> 中间件写入）。"""

# WebUI 开关名 -> 共享开关键
_SWITCH_KEYS: dict[str, str] = {
    # 接收开关：关闭 = 该类消息在接收管线最上游被直接拦截
    "recv_bilibili_live": "recv_bilibili_live",
    "recv_private": "recv_private",
    "recv_group": "recv_group",
    "understanding": "cyrene_understanding",
    "history_summary": "cyrene_history_summary",
    "rerank": "cyrene_rerank",
    "voice_reply": "cyrene_voice_reply",
    "group_voice_reply": "group_voice_reply",
    "proactive_context": "cyrene_proactive_context",
    "proactive_memory": "cyrene_proactive_memory",
    "asr_listening": "asr_listening",
}


class EntryCreatePayload(BaseModel):
    """新建语义条目请求体。"""

    text: str = Field(..., description="记忆文本")
    embed: bool = Field(default=True, description="是否调用 embedding 生成向量")


class ProfileSavePayload(BaseModel):
    """保存画像请求体。"""

    profile: dict[str, str] = Field(default_factory=dict, description="L0 画像键值")
    l1: str | None = Field(default=None, description="近期备忘 L1（传 None 表示不改）")


class SwitchSavePayload(BaseModel):
    """运行时功能开关请求体。"""

    switches: dict[str, bool] = Field(default_factory=dict, description="开关名 -> 是否启用")


class ConfigSavePayload(BaseModel):
    """全量配置保存请求体：{节名: {字段名: 值}}。"""

    values: dict[str, dict[str, Any]] = Field(default_factory=dict, description="配置节 -> 字段值")


class DfcProfilePayload(BaseModel):
    """DFC 画像保存请求体。"""

    profile: dict[str, str] = Field(default_factory=dict, description="DFC 画像键值")


class NeoChatterMemoryAdminRouter(BaseRouter):
    """cyrene_neochatter 分层记忆管理后台。"""

    name: str = "cyrene_neochatter_memory_admin"
    description: str = "Cyrene WebUI：cyrene 插件控制台（总览/功能开关/画像/配置/记忆）"
    custom_route_path: str = "/cyrene-webui"
    cors_origins: list[str] = ["*"]

    def __init__(self, plugin: Any) -> None:
        super().__init__(plugin)

    def _config(self) -> CyreneNeoConfig:
        cfg = getattr(self.plugin, "config", None)
        return cfg if isinstance(cfg, CyreneNeoConfig) else CyreneNeoConfig()

    def _dfc_root(self) -> str:
        """DFC（人格层）记忆的公共根目录。"""

        return self._config().memory_store.data_dir or "data/cyrene_memory"

    def _strat_root(self) -> str:
        """分层记忆的公共根目录。"""

        return self._config().memory.data_dir or "data/cyrene_neochatter"

    def _default_user(self) -> str:
        """没带 ?user= 时默认看谁：已分库的第一个人，退而用 memory.legacy_owner_id。"""

        users = user_scope.list_user_dirs(self._strat_root()) or user_scope.list_user_dirs(
            self._dfc_root()
        )
        if users:
            return users[0]
        return user_scope.legacy_owner(self._config())

    def _scope_user(self) -> str:
        """本次请求要看的私聊对象。"""

        return _CURRENT_USER.get() or self._default_user()

    def _dfc_base(self) -> str:
        """DFC（人格层）记忆目录（按 QQ 分库）。"""

        return user_scope.scope_dir(self._dfc_root(), self._scope_user())

    def _base(self) -> str:
        """分层记忆目录（按 QQ 分库）。"""

        return user_scope.scope_dir(self._strat_root(), self._scope_user())

    @staticmethod
    def _html_path() -> Path:
        return Path(__file__).with_name("memory_admin.html")

    def _load_html(self) -> str:
        try:
            return self._html_path().read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RuntimeError("memory_admin.html 不存在，无法加载管理后台") from exc

    async def _embed_text(self, text: str) -> list[float] | None:
        """给文本生成向量（失败返回 None，条目仍会保存只是不可语义召回）。"""
        try:
            from ..llm import Inference

            return await Inference(self._config(), self.plugin).embed(text)
        except Exception:
            return None

    @staticmethod
    def _type_name(annotation: Any) -> str:
        """把注解归一化成前端可用的类型名。"""
        origin = getattr(annotation, "__origin__", None)
        if origin is list:
            return "list"
        if origin is dict:
            return "dict"
        name = getattr(annotation, "__name__", str(annotation))
        return {"str": "str", "int": "int", "float": "float", "bool": "bool"}.get(name, "str")

    @staticmethod
    def _default_of(field_info: Any) -> Any:
        """取字段默认值（无默认返回 None）。"""
        try:
            from pydantic_core import PydanticUndefined

            if field_info.default is not PydanticUndefined:
                return field_info.default
        except Exception:
            pass
        factory = getattr(field_info, "default_factory", None)
        if callable(factory):
            try:
                return factory()
            except Exception:
                return None
        return None

    def _config_schema(self) -> dict[str, Any]:
        """按配置模型的元数据生成可渲染的完整配置结构。"""
        cfg = self._config()
        model = cfg.__class__
        sections: list[dict[str, Any]] = []
        for attr_name, field_info in model.model_fields.items():
            annotation = field_info.annotation
            if not (isinstance(annotation, type) and issubclass(annotation, SectionBase)):
                continue
            section_obj = getattr(cfg, attr_name)
            section_name = str(getattr(annotation, "__config_section_name__", attr_name) or attr_name)
            title = str(getattr(annotation, "__config_section_title__", "") or section_name)
            fields: list[dict[str, Any]] = []
            for fname, fmeta in annotation.model_fields.items():
                extra = dict(getattr(fmeta, "json_schema_extra", None) or {})
                if extra.get("hidden"):
                    continue
                type_name = self._type_name(fmeta.annotation)
                fields.append({
                    "name": fname,
                    "label": str(extra.get("label") or fname),
                    "description": str(fmeta.description or ""),
                    "type": type_name,
                    "input_type": str(extra.get("input_type") or ""),
                    "choices": extra.get("choices"),
                    "ge": extra.get("ge"),
                    "le": extra.get("le"),
                    "step": extra.get("step"),
                    "rows": extra.get("rows"),
                    "placeholder": extra.get("placeholder"),
                    "hint": extra.get("hint"),
                    "disabled": bool(extra.get("disabled")),
                    "value": getattr(section_obj, fname),
                    "default": self._default_of(fmeta),
                })
            sections.append({"name": section_name, "title": title, "fields": fields})
        return {"sections": sections}

    def _write_config(self, merged: dict[str, Any]) -> Any:
        """校验并用签名渲染写回 config/plugins/<plugin>/config.toml。"""
        config_cls = self._config().__class__
        new_cfg = config_cls.model_validate(merged)
        plugin_name = str(getattr(self.plugin, "plugin_name", "") or "cyrene_neochatter")
        path = Path("config") / "plugins" / plugin_name / "config.toml"
        from src.kernel.config.core import _render_toml_with_signature

        text = _render_toml_with_signature(config_cls, new_cfg.model_dump())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.plugin.config = new_cfg
        return new_cfg

    def register_endpoints(self) -> None:
        """注册后台页面与数据 API。"""

        @self.app.middleware("http")
        async def _bind_scope_user(request: Any, call_next: Any) -> Any:
            """把 ?user=<qq> 记进上下文变量：所有端点据此选用该 QQ 的分库目录。"""

            token = _CURRENT_USER.set(str(request.query_params.get("user") or ""))
            try:
                return await call_next(request)
            finally:
                _CURRENT_USER.reset(token)

        @self.app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def admin_page() -> HTMLResponse:
            """记忆管理主页。"""
            return HTMLResponse(self._load_html())

        def _switch_snapshot() -> dict[str, Any]:
            cfg = self._config()
            defaults = {
                "recv_bilibili_live": True,
                "recv_private": True,
                "recv_group": True,
                "understanding": bool(getattr(cfg.understanding, "enabled", True)),
                "history_summary": bool(getattr(cfg.context, "summary_enabled", True)),
                "rerank": bool(str(getattr(cfg.llm, "rerank_task", "") or "").strip()),
                "voice_reply": bool(getattr(cfg.voice, "enabled", False)),
                "group_voice_reply": False,
                "proactive_context": bool(getattr(cfg.proactive, "inject_context", True)),
                "proactive_memory": bool(getattr(cfg.proactive, "inject_memory", True)),
                "asr_listening": False,
            }
            snapshot: dict[str, Any] = {}
            for name, key in _SWITCH_KEYS.items():
                snapshot[name] = {
                    "key": key,
                    "effective": bool(get_switch(key, defaults[name])),
                    "config_default": defaults[name],
                }
            return snapshot

        @self.app.get("/api/config")
        async def get_config() -> dict[str, Any]:
            """返回全部可配置项（含元数据与当前值）。"""
            return self._config_schema()

        @self.app.post("/api/config")
        async def save_config(payload: ConfigSavePayload) -> dict[str, Any]:
            """保存全部配置（校验后按签名渲染写回 config.toml）。"""
            cfg = self._config()
            merged = cfg.model_dump()
            changed: list[str] = []
            for section_name, fields in (payload.values or {}).items():
                section = merged.get(str(section_name))
                if not isinstance(section, dict):
                    continue
                for field_name, value in (fields or {}).items():
                    if str(field_name) in section:
                        section[str(field_name)] = value
                        changed.append(str(section_name) + "." + str(field_name))
            if not changed:
                return {"ok": True, "changed": [], "sections": self._config_schema()["sections"]}
            try:
                self._write_config(merged)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"配置校验/写入失败: {exc}") from exc
            logger.info(f"cyrene_neochatter admin: 配置已保存 {len(changed)} 项")
            return {"ok": True, "changed": changed, "sections": self._config_schema()["sections"]}

        @self.app.get("/api/users")
        async def list_users() -> dict[str, Any]:
            """列出已分库的私聊对象（分层记忆与 DFC 记忆的并集）。"""

            strat_root = self._strat_root()
            dfc_root = self._dfc_root()
            ids: list[str] = []
            for user_id in user_scope.list_user_dirs(strat_root) + user_scope.list_user_dirs(dfc_root):
                if user_id not in ids:
                    ids.append(user_id)
            items = [
                {
                    "user_id": user_id,
                    "label": user_scope.user_label(strat_root, user_id),
                    "has_strat": (Path(user_scope.scope_dir(strat_root, user_id)) / "memory.json").is_file(),
                    "has_dfc": (Path(user_scope.scope_dir(dfc_root, user_id)) / "memory.json").is_file(),
                }
                for user_id in ids
            ]
            return {"users": items, "default": self._default_user()}

        @self.app.get("/api/switches")
        async def list_switches() -> dict[str, Any]:
            """返回运行时功能开关状态。"""
            return _switch_snapshot()

        @self.app.post("/api/switches")
        async def save_switches(payload: SwitchSavePayload) -> dict[str, Any]:
            """设置运行时功能开关（立即生效，重启保留）。"""
            changed: list[str] = []
            for name, value in (payload.switches or {}).items():
                key = _SWITCH_KEYS.get(str(name))
                if key:
                    set_switch(key, bool(value))
                    changed.append(str(name))
            if changed:
                logger.info(f"cyrene_neochatter admin: 功能开关已更新 {changed}")
            return _switch_snapshot()

        # ---------- DFC（群聊/人格层）记忆：data/cyrene_memory ----------

        @self.app.get("/api/dfc/overview")
        async def dfc_overview() -> dict[str, Any]:
            """DFC 侧记忆总览。"""
            data = dfc_memory_store.load_memory(self._dfc_base())
            return {
                "entries": len(data.get("entries") or []),
                "relationship": len(data.get("relationship") or []),
                "worldbook": len(data.get("worldbook") or []),
                "profile_keys": len(data.get("profile") or {}),
                "meta": data.get("meta") or {},
            }

        @self.app.get("/api/dfc/entries")
        async def dfc_list_entries(
            keyword: str | None = Query(default=None, description="关键词"),
            limit: int = Query(default=500, ge=1, le=2000),
        ) -> dict[str, Any]:
            data = dfc_memory_store.load_memory(self._dfc_base())
            items = list(data.get("entries") or [])
            if keyword:
                kw = keyword.strip().lower()
                items = [e for e in items if kw in str(e.get("text") or "").lower()]
            out = []
            for e in items[-limit:]:
                emb = e.get("embedding")
                out.append({
                    "id": e.get("id"),
                    "text": e.get("text"),
                    "source": e.get("source"),
                    "createdAt": e.get("createdAt"),
                    "dim": len(emb) if isinstance(emb, list) else 0,
                })
            return {"items": out}

        @self.app.post("/api/dfc/entries")
        async def dfc_create_entry(payload: EntryCreatePayload) -> dict[str, Any]:
            text = (payload.text or "").strip()
            if not text:
                raise HTTPException(status_code=400, detail="文本不能为空")
            data = dfc_memory_store.load_memory(self._dfc_base())
            entry: dict[str, Any] = {
                "id": "manual_" + str(time.time_ns()),
                "text": text,
                "source": "manual",
                "weight": 1.0,
                "createdAt": time.time(),
                "metadata": {},
            }
            if payload.embed:
                try:
                    entry["embedding"] = await dfc_memory_store.embed_text(text)
                except Exception as exc:
                    raise HTTPException(status_code=502, detail=f"embedding 失败（未保存）: {exc}") from exc
            data.setdefault("entries", []).append(entry)
            dfc_memory_store.save_memory(self._dfc_base(), data)
            return {"ok": True, "id": entry["id"], "embedded": payload.embed}

        @self.app.delete("/api/dfc/entries/{entry_id}")
        async def dfc_delete_entry(entry_id: str) -> dict[str, Any]:
            data = dfc_memory_store.load_memory(self._dfc_base())
            before = len(data.get("entries") or [])
            data["entries"] = [e for e in (data.get("entries") or []) if str(e.get("id")) != str(entry_id)]
            dfc_memory_store.save_memory(self._dfc_base(), data)
            return {"ok": True, "removed": before - len(data["entries"])}

        @self.app.get("/api/dfc/relationship")
        async def dfc_list_relationship(limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
            data = dfc_memory_store.load_memory(self._dfc_base())
            items = data.get("relationship") or []
            start = max(0, len(items) - limit)
            out = []
            for i in range(start, len(items)):
                rec = items[i]
                out.append({
                    "index": i,
                    "date": rec.get("date"),
                    "userText": rec.get("userText"),
                    "userMood": rec.get("userMood"),
                    "importantMoment": rec.get("importantMoment"),
                    "nextCareCue": rec.get("nextCareCue"),
                })
            return {"items": out}

        @self.app.delete("/api/dfc/relationship")
        async def dfc_delete_relationship(
            index: int = Query(..., description="关系记录下标（全局下标）"),
        ) -> dict[str, Any]:
            data = dfc_memory_store.load_memory(self._dfc_base())
            items = data.get("relationship") or []
            if index < 0 or index >= len(items):
                raise HTTPException(status_code=404, detail="下标越界")
            removed = items.pop(index)
            dfc_memory_store.save_memory(self._dfc_base(), data)
            return {"ok": True, "removed_date": removed.get("date")}

        @self.app.get("/api/dfc/worldbook")
        async def dfc_list_worldbook(keyword: str | None = Query(default=None)) -> dict[str, Any]:
            data = dfc_memory_store.load_memory(self._dfc_base())
            items = list(data.get("worldbook") or [])
            if keyword:
                kw = keyword.strip().lower()
                items = [w for w in items if kw in str(w.get("name") or "").lower() or any(kw in str(t).lower() for t in (w.get("triggers") or []))]
            return {"items": [{"name": w.get("name"), "file": w.get("file"), "triggers": w.get("triggers"), "resident": w.get("resident"), "content": str(w.get("content") or "")[:300]} for w in items]}

        @self.app.delete("/api/dfc/worldbook")
        async def dfc_delete_worldbook(name: str = Query(..., description="WorldBook 条目名")) -> dict[str, Any]:
            data = dfc_memory_store.load_memory(self._dfc_base())
            before = len(data.get("worldbook") or [])
            target = str(name).strip()
            data["worldbook"] = [w for w in (data.get("worldbook") or []) if str(w.get("name")) != target]
            dfc_memory_store.save_memory(self._dfc_base(), data)
            return {"ok": True, "removed": before - len(data["worldbook"])}

        @self.app.get("/api/dfc/profile")
        async def dfc_get_profile() -> dict[str, Any]:
            data = dfc_memory_store.load_memory(self._dfc_base())
            return {"profile": data.get("profile") or {}}

        @self.app.post("/api/dfc/profile")
        async def dfc_save_profile(payload: DfcProfilePayload) -> dict[str, Any]:
            data = dfc_memory_store.load_memory(self._dfc_base())
            clean = {str(k): str(v).strip() for k, v in (payload.profile or {}).items() if str(v).strip()}
            data["profile"] = clean
            dfc_memory_store.save_memory(self._dfc_base(), data)
            return {"ok": True, "profile": clean}

        @self.app.post("/api/dfc/import/refresh")
        async def dfc_import_refresh() -> dict[str, Any]:
            cfg = self._config()
            source = (cfg.memory_store.source_dir or "").strip() or dfc_memory_store.default_source_dir()
            wb = (getattr(cfg.memory_store, "worldbook_dir", "") or "").strip() or None
            if not source or not Path(source).is_dir():
                raise HTTPException(status_code=400, detail="未配置有效的 Cyrene 数据源")
            return dfc_memory_store.migrate(source, self._dfc_base(), worldbook_dir=wb)

        @self.app.get("/api/profile")
        async def get_profile() -> dict[str, Any]:
            """返回 L0 画像与 L1 近期备忘。"""
            data = load_store(self._base())
            return {"l0": data.get("l0") or {}, "l1": str(data.get("l1") or "")}

        @self.app.post("/api/profile")
        async def save_profile(payload: ProfileSavePayload) -> dict[str, Any]:
            """保存 L0 画像（合并写入）与 L1 近期备忘。"""
            data = load_store(self._base())
            l0 = data.setdefault("l0", {})
            changed = 0
            for key, value in (payload.profile or {}).items():
                name = str(key or "").strip()
                if not name:
                    continue
                text = str(value or "").strip()
                if text:
                    l0[name] = text
                    changed += 1
                else:
                    l0.pop(name, None)
            if payload.l1 is not None:
                data["l1"] = str(payload.l1 or "").strip()
                changed += 1
            if changed:
                save_store(self._base(), data)
                logger.info(f"cyrene_neochatter admin: 画像已更新 {changed} 项")
            return {"l0": l0, "l1": str(data.get("l1") or ""), "changed": changed}

        @self.app.get("/api/overview")
        async def overview() -> dict[str, Any]:
            """返回记忆总览统计。"""
            data = load_store(self._base())
            rel = data.get("relationship") or []
            ent = data.get("entries") or []

            def _src_counter(items: list[dict[str, Any]]) -> dict[str, int]:
                counter: dict[str, int] = {}
                for item in items:
                    key = str(item.get("source") or "migrated")
                    counter[key] = counter.get(key, 0) + 1
                return counter

            return {
                "l0": data.get("l0") or {},
                "l1": str(data.get("l1") or ""),
                "relationship_total": len(rel),
                "relationship_sources": _src_counter(rel),
                "entries_total": len(ent),
                "entries_sources": _src_counter(ent),
                "worldbook": len(data.get("worldbook") or []),
                "meta": data.get("meta") or {},
            }

        @self.app.get("/api/relationship")
        async def list_relationship(
            keyword: str | None = Query(default=None, description="关键词"),
            source: str | None = Query(default=None, description="来源过滤：auto / migrated / chat_history"),
            limit: int = Query(default=200, ge=1, le=1000),
        ) -> dict[str, Any]:
            data = load_store(self._base())
            items = list(data.get("relationship") or [])
            if source:
                items = [r for r in items if str(r.get("source") or "migrated") == source]
            if keyword:
                kw = keyword.strip().lower()
                items = [r for r in items if kw in str(r.get("userText") or "").lower() or kw in str(r.get("nextCareCue") or "").lower()]
            return {"items": items[-limit:] or items}

        @self.app.get("/api/entries")
        async def list_entries(
            keyword: str | None = Query(default=None, description="关键词"),
            source: str | None = Query(default=None, description="来源过滤：auto / chat_history / migrated"),
            limit: int = Query(default=200, ge=1, le=1000),
        ) -> dict[str, Any]:
            data = load_store(self._base())
            items = list(data.get("entries") or [])
            if source:
                items = [e for e in items if str(e.get("source") or "?") == source]
            if keyword:
                kw = keyword.strip().lower()
                items = [e for e in items if kw in str(e.get("text") or "").lower()]
            out = []
            for e in (items[-limit:] if limit else items):
                emb = e.get("embedding")
                out.append({
                    "id": e.get("id"),
                    "text": e.get("text"),
                    "source": e.get("source"),
                    "createdAt": e.get("createdAt"),
                    "dim": len(emb) if isinstance(emb, list) else 0,
                })
            return {"items": out}

        @self.app.post("/api/entries")
        async def create_entry(payload: EntryCreatePayload) -> dict[str, Any]:
            """手工新增一条语义条目（可选生成向量）。"""
            text = (payload.text or "").strip()
            if not text:
                raise HTTPException(status_code=400, detail="文本不能为空")
            data = load_store(self._base())
            emb = await self._embed_text(text) if payload.embed else None
            entry: dict[str, Any] = {
                "id": "auto_" + str(int(time.time() * 1000)),
                "text": text,
                "source": "auto",
                "weight": 1.0,
                "createdAt": time.time(),
                "metadata": {},
            }
            if emb:
                entry["embedding"] = emb
            data.setdefault("entries", []).append(entry)
            save_store(self._base(), data)
            return {"ok": True, "id": entry["id"], "dim": len(emb) if emb else 0}

        @self.app.delete("/api/entries/{entry_id}")
        async def delete_entry(entry_id: str) -> dict[str, Any]:
            """按 id 删除一条语义条目。"""
            data = load_store(self._base())
            items = list(data.get("entries") or [])
            kept = [e for e in items if str(e.get("id") or "") != entry_id]
            if len(kept) == len(items):
                raise HTTPException(status_code=404, detail="未找到该条目")
            data["entries"] = kept
            save_store(self._base(), data)
            return {"ok": True}

        @self.app.delete("/api/relationship")
        async def delete_relationship(
            index: int = Query(..., description="关系记录下标（列表顺序，0 为最早）"),
        ) -> dict[str, Any]:
            data = load_store(self._base())
            items = list(data.get("relationship") or [])
            if index < 0 or index >= len(items):
                raise HTTPException(status_code=404, detail="下标越界")
            data["relationship"] = items[:index] + items[index + 1:]
            save_store(self._base(), data)
            return {"ok": True}


__all__ = ["NeoChatterMemoryAdminRouter"]
