"""No-key public web search and SSRF-resistant text extraction."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from html import unescape
from importlib.util import find_spec
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx


class WebAccessError(RuntimeError):
    pass


def normalize_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise WebAccessError("只允许访问有效的 HTTP/HTTPS URL")
    if parsed.username or parsed.password:
        raise WebAccessError("URL 不允许包含认证信息")
    normalized = parsed._replace(scheme=parsed.scheme.lower(), fragment="")
    return urlunparse(normalized)


def _blocked_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not address.is_global


async def validate_public_url(value: str) -> str:
    normalized = normalize_url(value)
    hostname = urlparse(normalized).hostname
    assert hostname is not None
    if hostname.lower() == "localhost" or hostname.lower().endswith(".localhost"):
        raise WebAccessError("禁止访问本机地址")
    try:
        records = await asyncio.to_thread(socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebAccessError(f"域名解析失败: {hostname}") from exc
    addresses = {record[4][0].split("%")[0] for record in records}
    if not addresses or any(_blocked_ip(address) for address in addresses):
        raise WebAccessError("禁止访问私网、回环或保留地址")
    return normalized


def _plain_text_fallback(html: str) -> str:
    without_scripts = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", without_scripts)
    return re.sub(r"\s+", " ", unescape(text)).strip()


@dataclass(slots=True)
class WebPage:
    url: str
    title: str
    text: str
    content_type: str


class WebResearchService:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_bytes: int,
        timeout_seconds: float,
        search_timeout_seconds: float | None = None,
    ) -> None:
        self.client = client
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self.search_timeout_seconds = search_timeout_seconds or timeout_seconds

    @property
    def search_available(self) -> bool:
        return find_spec("ddgs") is not None

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        if not self.search_available:
            raise WebAccessError("未安装 ddgs，网页搜索能力不可用")

        def execute() -> list[dict[str, str]]:
            from ddgs import DDGS

            rows = DDGS(timeout=self.timeout_seconds).text(query, max_results=max_results)
            return list(rows or [])

        try:
            raw_results = await asyncio.wait_for(
                asyncio.to_thread(execute), timeout=self.search_timeout_seconds
            )
        except TimeoutError as exc:
            raise WebAccessError(
                f"DDGS 搜索超过 {self.search_timeout_seconds:g} 秒，已停止本次请求"
            ) from exc
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise WebAccessError(f"DDGS 搜索失败: {detail}") from exc
        output: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw_results:
            href = str(item.get("href") or item.get("url") or "").strip()
            try:
                href = normalize_url(href)
            except WebAccessError:
                continue
            key = href.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            output.append(
                {
                    "title": str(item.get("title") or href),
                    "url": href,
                    "snippet": str(item.get("body") or item.get("snippet") or ""),
                }
            )
        return output[:max_results]

    async def fetch(self, value: str) -> WebPage:
        url = await validate_public_url(value)
        redirects = 0
        while True:
            try:
                async with self.client.stream(
                    "GET",
                    url,
                    headers={"User-Agent": "ResearchAgent/0.1 (+local research tool)"},
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or redirects >= 3:
                            raise WebAccessError("网页重定向次数过多或缺少目标")
                        url = await validate_public_url(urljoin(url, location))
                        redirects += 1
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
                        raise WebAccessError(f"V1 不支持抓取该内容类型: {content_type or 'unknown'}")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise WebAccessError("网页响应超过允许的大小")
                        chunks.append(chunk)
                    encoding = response.encoding or "utf-8"
                    raw = b"".join(chunks).decode(encoding, errors="replace")
                    break
            except WebAccessError:
                raise
            except httpx.HTTPError as exc:
                detail = str(exc).strip() or type(exc).__name__
                raise WebAccessError(f"网页抓取失败: {detail}") from exc
        title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, flags=re.I | re.S)
        title = re.sub(r"\s+", " ", unescape(title_match.group(1))).strip() if title_match else url
        text = ""
        try:
            import trafilatura

            text = trafilatura.extract(raw, url=url, include_comments=False, include_tables=True) or ""
        except Exception:
            text = ""
        text = re.sub(r"\n{3,}", "\n\n", text.strip()) or _plain_text_fallback(raw)
        if not text:
            raise WebAccessError("网页未提取到可用正文")
        return WebPage(url=url, title=title[:500], text=text[:30_000], content_type=content_type)
