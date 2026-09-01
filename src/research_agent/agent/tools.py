"""Agent tools that emit durable progress and normalize all evidence."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from langchain_core.tools import tool

from research_agent.agent.context import get_tool_context
from research_agent.services.paperlens import PaperLensError, normalize_paperlens_evidence
from research_agent.services.web import WebAccessError


def _source_for_model(source) -> dict:
    payload = source.model_dump()
    payload.pop("run_id", None)
    payload.pop("retrieved_at", None)
    payload["citation"] = f"[{source.source_id}]"
    return payload


async def _search_local_papers_impl(
    query: Annotated[str, "针对已选论文的具体检索问题"],
    top_k: Annotated[int, "返回证据数量，1 到 10"] = 6,
) -> str:
    context = get_tool_context()
    existing_count = context.database.count_sources(context.run_id, "local_paper")
    if existing_count >= context.settings.max_local_sources:
        message = f"已取得 {existing_count} 条论文证据，达到证据数量上限。请停止检索并生成回答。"
        context.database.add_event(context.run_id, "evidence_sufficient", "local", message)
        return json.dumps({"error": "evidence_sufficient", "fatal": True, "message": message}, ensure_ascii=False)
    budget_error = context.use_budget("论文检索", context.settings.max_local_searches)
    if budget_error:
        context.database.add_event(context.run_id, "budget_exhausted", "local", budget_error)
        return json.dumps({"error": "budget_exhausted", "fatal": True, "message": budget_error}, ensure_ascii=False)
    if not context.document_ids:
        return json.dumps({"error": "本轮没有选择本地论文", "fatal": True}, ensure_ascii=False)
    if context.database.get_counter(context.run_id, "paperlens_failures") >= 1:
        message = "PaperLens 本轮请求已经失败，本地检索已熔断。不得重试，请使用已有证据完成回答。"
        context.database.add_event(context.run_id, "circuit_open", "local", message)
        return json.dumps({"error": "paperlens_circuit_open", "fatal": True, "message": message}, ensure_ascii=False)
    context.database.add_event(context.run_id, "tool_start", "local", f"检索本地论文：{query}", {"query": query})
    async def report_waiting() -> None:
        elapsed = 0
        while True:
            await asyncio.sleep(20)
            elapsed += 20
            context.database.add_event(
                context.run_id,
                "tool_progress",
                "local",
                f"PaperLens 正在加载模型或检索证据（已等待 {elapsed} 秒）",
                {"elapsed_seconds": elapsed, "query": query},
            )

    heartbeat = asyncio.create_task(report_waiting())
    try:
        payload = await asyncio.wait_for(
            context.paperlens.evidence_search(
                query.strip(), context.document_ids, top_k=max(1, min(top_k, 5))
            ),
            timeout=max(0.1, context.remaining_evidence_seconds()),
        )
    except TimeoutError:
        message = "论文检索已到本轮证据截止时间。请立即使用已有证据生成回答。"
        context.database.add_event(context.run_id, "evidence_deadline", "local", message)
        return json.dumps({"error": "evidence_deadline", "fatal": True, "message": message}, ensure_ascii=False)
    except PaperLensError as exc:
        failures = context.database.increment_counter(context.run_id, "paperlens_failures")
        context.database.add_event(context.run_id, "tool_error", "local", str(exc))
        return json.dumps({
            "error": "paperlens_unavailable",
            "fatal": True,
            "message": str(exc),
            "consecutive_failures": failures,
        }, ensure_ascii=False)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
    context.database.reset_counter(context.run_id, "paperlens_failures")
    sources = []
    for item in payload["evidence"]:
        if context.database.count_sources(context.run_id, "local_paper") >= context.settings.max_local_sources:
            break
        source, created = context.database.add_source(context.run_id, normalize_paperlens_evidence(item))
        sources.append(_source_for_model(source))
        if created:
            context.database.add_event(
                context.run_id,
                "source_found",
                "local",
                f"找到论文证据 [{source.source_id}] {source.title}",
                {"source_id": source.source_id, "document_id": source.document_id, "page": source.page},
            )
    return json.dumps({"query": query, "sources": sources}, ensure_ascii=False)


@tool
async def search_local_papers(
    query: Annotated[str, "针对已选论文的具体检索问题"],
    top_k: Annotated[int, "返回证据数量，1 到 10"] = 6,
) -> str:
    """Search selected PaperLens documents and return attributed original passages."""
    context = get_tool_context()
    async with context.local_search_lock:
        return await _search_local_papers_impl(query, top_k)


@tool
async def search_web(
    query: Annotated[str, "公开网页搜索关键词或问题"],
    max_results: Annotated[int, "候选网页数量，1 到 8"] = 5,
) -> str:
    """Search the public web without an API key and return candidate URLs."""
    context = get_tool_context()
    route_error = context.web_route_error()
    if route_error:
        context.database.add_event(context.run_id, "route_stopped", "web", route_error)
        return json.dumps({"error": "web_not_needed", "fatal": True, "message": route_error}, ensure_ascii=False)
    if context.database.get_counter(context.run_id, "web_search_failures") >= 2:
        message = "DDGS 本轮已连续失败两次，网页搜索已熔断。请使用已有证据完成回答。"
        context.database.add_event(context.run_id, "circuit_open", "web", message)
        return json.dumps({"error": "web_search_circuit_open", "fatal": True, "message": message}, ensure_ascii=False)
    budget_error = context.use_budget("网页搜索", context.settings.max_web_searches)
    if budget_error:
        context.database.add_event(context.run_id, "budget_exhausted", "web", budget_error)
        return json.dumps({"error": "budget_exhausted", "fatal": True, "message": budget_error}, ensure_ascii=False)
    context.database.add_event(context.run_id, "tool_start", "web", f"搜索公开网页：{query}", {"query": query})
    try:
        results = await asyncio.wait_for(
            context.web.search(query.strip(), max_results=max(1, min(max_results, 8))),
            timeout=max(0.1, context.remaining_evidence_seconds()),
        )
    except TimeoutError:
        message = "网页搜索已到本轮证据截止时间。请使用已有证据完成回答。"
        context.database.add_event(context.run_id, "evidence_deadline", "web", message)
        return json.dumps({"error": "evidence_deadline", "fatal": True, "message": message}, ensure_ascii=False)
    except WebAccessError as exc:
        failures = context.database.increment_counter(context.run_id, "web_search_failures")
        context.database.add_event(context.run_id, "tool_error", "web", str(exc))
        return json.dumps({
            "error": "web_search_failed",
            "fatal": failures >= 2,
            "message": str(exc),
            "consecutive_failures": failures,
        }, ensure_ascii=False)
    context.database.reset_counter(context.run_id, "web_search_failures")
    context.database.add_event(
        context.run_id, "search_results", "web", f"获得 {len(results)} 个网页候选", {"results": results}
    )
    return json.dumps({"query": query, "results": results}, ensure_ascii=False)


@tool
async def fetch_web_page(
    url: Annotated[str, "search_web 返回的公开网页 URL"],
    relevance: Annotated[str, "该网页与研究问题的关系"] = "",
) -> str:
    """Safely fetch a public HTML page and register its extracted text as evidence."""
    context = get_tool_context()
    route_error = context.web_route_error()
    if route_error:
        context.database.add_event(context.run_id, "route_stopped", "web", route_error)
        return json.dumps({"error": "web_not_needed", "fatal": True, "message": route_error}, ensure_ascii=False)
    budget_error = context.use_budget("网页抓取", context.settings.max_web_fetches)
    if budget_error:
        context.database.add_event(context.run_id, "budget_exhausted", "web", budget_error)
        return json.dumps({"error": "budget_exhausted", "fatal": True, "message": budget_error}, ensure_ascii=False)
    context.database.add_event(context.run_id, "tool_start", "web", f"读取网页：{url}", {"url": url})
    try:
        page = await asyncio.wait_for(
            context.web.fetch(url), timeout=max(0.1, context.remaining_evidence_seconds())
        )
    except TimeoutError:
        message = "网页抓取已到本轮证据截止时间。请使用已有证据完成回答。"
        context.database.add_event(context.run_id, "evidence_deadline", "web", message, {"url": url})
        return json.dumps({"error": "evidence_deadline", "fatal": True, "message": message}, ensure_ascii=False)
    except WebAccessError as exc:
        context.database.add_event(context.run_id, "tool_error", "web", str(exc), {"url": url})
        return json.dumps({"error": str(exc), "url": url}, ensure_ascii=False)
    source, created = context.database.add_source(
        context.run_id,
        {
            "kind": "web",
            "title": page.title,
            "url": page.url,
            "excerpt": page.text,
            "metadata": {"content_type": page.content_type, "relevance": relevance},
        },
    )
    if created:
        context.database.add_event(
            context.run_id,
            "source_found",
            "web",
            f"读取网页来源 [{source.source_id}] {source.title}",
            {"source_id": source.source_id, "url": source.url},
        )
    return json.dumps(_source_for_model(source), ensure_ascii=False)
