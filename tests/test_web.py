import httpx
import pytest
import asyncio

import research_agent.services.web as web_module
from research_agent.services.web import WebAccessError, WebResearchService, normalize_url


def test_normalize_url_rejects_non_http_and_credentials() -> None:
    with pytest.raises(WebAccessError):
        normalize_url("file:///etc/passwd")
    with pytest.raises(WebAccessError):
        normalize_url("https://user:password@example.com/private")
    assert normalize_url("HTTPS://Example.com/a#fragment") == "https://Example.com/a"


@pytest.mark.asyncio
async def test_fetch_extracts_html_and_limits_content(monkeypatch) -> None:
    async def public(value: str) -> str:
        return value

    monkeypatch.setattr(web_module, "validate_public_url", public)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text="""
          <html><head><title>Research result</title></head><body><main><h1>Finding</h1>
          <p>This is a sufficiently informative public research result with evidence.</p></main></body></html>
        """)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = WebResearchService(client, max_bytes=10_000, timeout_seconds=3)
        page = await service.fetch("https://example.com/research")
    assert page.title == "Research result"
    assert "informative" in page.text


@pytest.mark.asyncio
async def test_search_has_strict_outer_timeout(monkeypatch) -> None:
    async def slow_to_thread(_function):
        await asyncio.sleep(0.05)
        return []

    monkeypatch.setattr(web_module.asyncio, "to_thread", slow_to_thread)
    async with httpx.AsyncClient() as client:
        service = WebResearchService(
            client, max_bytes=10_000, timeout_seconds=3, search_timeout_seconds=0.01
        )
        monkeypatch.setattr(
            WebResearchService,
            "search_available",
            property(lambda _self: True),
        )
        with pytest.raises(WebAccessError, match="超过"):
            await service.search("fixture")
