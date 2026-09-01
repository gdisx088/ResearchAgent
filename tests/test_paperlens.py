import asyncio

import httpx
import pytest

from research_agent.services.paperlens import PaperLensClient, normalize_paperlens_evidence


@pytest.mark.asyncio
async def test_paperlens_client_sends_workspace_and_normalizes_evidence() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Workspace-Id"] == "workspace-a"
        assert request.url.path == "/api/v1/agent/evidence-search"
        return httpx.Response(200, json={"evidence": [{
            "citation_id": "doc-a:b1", "document_id": "doc-a", "document_title": "Paper A",
            "block_id": "b1", "excerpt": "Grounded text", "section": "Methods", "page": 2,
            "page_end": 2, "page_label": "2", "bboxes": [], "retrieval_sources": ["dense"],
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = PaperLensClient(client, "http://paperlens", "workspace-a")
        payload = await service.evidence_search("method", ["doc-a"])
    normalized = normalize_paperlens_evidence(payload["evidence"][0])
    assert normalized["kind"] == "local_paper"
    assert normalized["document_id"] == "doc-a"
    assert normalized["page"] == 2
    assert normalized["metadata"]["citation_id"] == "doc-a:b1"


@pytest.mark.asyncio
async def test_evidence_requests_are_serialized() -> None:
    active = 0
    peak = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, json={"evidence": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = PaperLensClient(client, "http://paperlens", "workspace-a")
        await asyncio.gather(
            service.evidence_search("one", ["doc-a"]),
            service.evidence_search("two", ["doc-a"]),
            service.evidence_search("three", ["doc-a"]),
        )
    assert peak == 1
