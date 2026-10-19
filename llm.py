"""推理接口：全走 MoFox API（LLM/embedding/可选 rerank），预留本地接口位。

backend=api：调用 src.app.plugin_system.api.llm_api（模型任务来自 config）。
backend=local：本地推理接口位——未来接 onnx/LanceDB 等，当前调用会抛 NotImplementedError。
"""

from __future__ import annotations

import asyncio
from typing import Any


class LocalNotReadyError(NotImplementedError):
    pass


class Inference:
    """统一推理门面：chat / embed / rerank。"""

    def __init__(self, cfg: Any, plugin: Any = None) -> None:
        self._cfg = cfg
        self._plugin = plugin

    @property
    def backend(self) -> str:
        return str(getattr(getattr(self._cfg, "llm", None), "backend", "api") or "api").strip().lower()

    async def chat(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        """单轮文本对话，返回 assistant 文本。"""
        if self.backend != "api":
            raise LocalNotReadyError("backend=local 预留接口尚未实现，请改用 api")
        from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
        from src.kernel.llm import LLMPayload, ROLE, Text

        task = getattr(self._cfg.general, "model_task", "actor") or "actor"
        cap = int(max_tokens or getattr(self._cfg.llm, "max_tokens", 8000) or 8000)
        request = create_llm_request(get_model_set_by_task(task), request_name="cyrene_neochatter")
        # LLMRequest 是 slots dataclass，没有 max_tokens 字段；改在各模型条目上钳制上限
        if cap and cap > 0:
            request.model_set = [
                dict(m, max_tokens=min(int(m.get("max_tokens") or cap), cap))
                for m in request.model_set
            ]
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system)))
        request.add_payload(LLMPayload(ROLE.USER, Text(user)))
        response = await request.send(stream=False)
        await response
        return str(getattr(response, "message", "") or "")

    async def embed(self, text: str) -> list[float]:
        """单条文本向量。"""
        if self.backend != "api":
            raise LocalNotReadyError("backend=local 预留接口尚未实现，请改用 api")
        from src.app.plugin_system.api.llm_api import create_embedding_request, get_model_set_by_task

        task = getattr(self._cfg.llm, "embed_task", "embedding") or "embedding"
        request = create_embedding_request(model_set=get_model_set_by_task(task), request_name="cyrene_neochatter_embed", inputs=[text])
        response = await request.send()
        emb = getattr(response, "embeddings", None) or []
        if not emb:
            raise RuntimeError("embedding 返回为空")
        return [float(v) for v in emb[0]]

    async def rerank(self, query: str, documents: list[str], top_n: int | None = None) -> list[float] | None:
        """可选 rerank；任务名为空或调用失败返回 None（上层用余弦顺序兜底）。"""
        task = str(getattr(self._cfg.llm, "rerank_task", "") or "").strip()
        if not task or self.backend != "api":
            return None
        try:
            from src.app.plugin_system.api.llm_api import create_rerank_request, get_model_set_by_task

            request = create_rerank_request(get_model_set_by_task(task), request_name="cyrene_neochatter_rerank", query=query, documents=documents, top_n=top_n)
            response = await request.send()
            results = getattr(response, "results", None)
            if not results:
                return None
            # RerankResponse.results 是 RerankItem（index/score/document）：
            # 按输入下标回填分数，返回与 documents 等长的列表，便于上层对齐候选。
            scores = [0.0] * len(documents)
            for item in results:
                index = getattr(item, "index", None)
                score = getattr(item, "score", None)
                if isinstance(index, int) and 0 <= index < len(scores) and score is not None:
                    scores[index] = float(score)
            return scores if any(scores) else None
        except Exception:
            return None


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
