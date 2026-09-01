"""Typed asynchronous client for the separately running PaperLens service."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx


class PaperLensError(RuntimeError):
    pass


class PaperLensClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        workspace_id: str,
        evidence_timeout_seconds: float = 180,
        reranker_mode: str = "off",
    ) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.workspace_id = workspace_id
        self.evidence_timeout_seconds = evidence_timeout_seconds
        self.reranker_mode = reranker_mode
        self._evidence_lock = asyncio.Lock()

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Workspace-Id": self.workspace_id}

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self.headers, **kwargs.pop("headers", {})}
        try:
            response = await self.client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:1000]
            raise PaperLensError(f"PaperLens HTTP {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            detail = str(exc).strip() or "请求超时或连接中断"
            raise PaperLensError(f"无法连接 PaperLens ({type(exc).__name__}): {detail}") from exc

    async def health(self) -> bool:
        try:
            response = await self._request("GET", "/api/v1/health")
            return response.json().get("status") == "ok"
        except (PaperLensError, ValueError):
            return False

    async def list_documents(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/api/v1/documents")
        payload = response.json()
        if not isinstance(payload, list):
            raise PaperLensError("PaperLens 文档列表格式无效")
        return payload

    async def upload_document(self, filename: str, content: bytes, content_type: str | None) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/api/v1/documents",
            files={"file": (filename, content, content_type or "application/octet-stream")},
        )
        return response.json()

    async def update_document(self, document_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._request("PATCH", f"/api/v1/documents/{document_id}", json=payload)
        return response.json()

    async def delete_document(self, document_id: str) -> None:
        await self._request("DELETE", f"/api/v1/documents/{document_id}")

    async def reindex_document(self, document_id: str) -> dict[str, Any]:
        response = await self._request("POST", f"/api/v1/documents/{document_id}/reindex")
        return response.json()

    async def page_preview(self, document_id: str, page: int) -> tuple[bytes, str]:
        response = await self._request("GET", f"/api/v1/documents/{document_id}/pages/{page}")
        return response.content, response.headers.get("content-type", "image/png")

    async def evidence_search(
        self,
        query: str,
        document_ids: list[str],
        *,
        top_k: int = 8,
    ) -> dict[str, Any]:
        # PaperLens uses local embedding/reranking models. Serialize evidence calls so
        # parallel subagents cannot overload that synchronous inference path.
        async with self._evidence_lock:
            response = await self._request(
                "POST",
                "/api/v1/agent/evidence-search",
                json={
                    "query": query,
                    "document_ids": document_ids,
                    "top_k": top_k,
                    "reranker_mode": self.reranker_mode,
                },
                timeout=self.evidence_timeout_seconds,
            )
        payload = response.json()
        if not isinstance(payload.get("evidence"), list):
            raise PaperLensError("PaperLens 证据检索响应缺少 evidence")
        return payload

    async def stream_job_events(self, job_id: str) -> AsyncIterator[bytes]:
        try:
            async with self.client.stream(
                "GET",
                f"{self.base_url}/api/v1/jobs/{job_id}/events",
                headers=self.headers,
                timeout=None,
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.HTTPError as exc:
            raise PaperLensError(f"PaperLens 索引事件流异常: {exc}") from exc


def normalize_paperlens_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """Translate PaperLens' attributed evidence contract into SourceRecord input."""
    return {
        "kind": "local_paper",
        "title": item.get("document_title") or "本地论文",
        "document_id": item.get("document_id"),
        "block_id": item.get("block_id"),
        "page": item.get("page"),
        "page_end": item.get("page_end"),
        "section": item.get("section") or None,
        "excerpt": item.get("excerpt") or "",
        "metadata": {
            "citation_id": item.get("citation_id"),
            "content_type": item.get("content_type"),
            "page_label": item.get("page_label"),
            "bboxes": item.get("bboxes") or [],
            "fused_score": item.get("fused_score"),
            "rerank_score": item.get("rerank_score"),
            "retrieval_sources": item.get("retrieval_sources") or [],
        },
    }
