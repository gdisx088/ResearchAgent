"""Agent tools that emit durable progress and normalize all evidence."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Literal

from langchain_core.tools import tool

from research_agent.agent.context import get_tool_context
from research_agent.services.evidence import (
    evidence_coverage,
    paper_explanation_requested,
    paperlens_rejection_reason,
)
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
    top_k: Annotated[int, "返回候选证据数量，1 到 12"] = 8,
    search_mode: Annotated[Literal["high_quality", "fast"], "高质量重排或快速混合检索"] = "high_quality",
) -> str:
    context = get_tool_context()
    retrieval_error = context.retrieval_error()
    if retrieval_error:
        context.database.add_event(context.run_id, "retrieval_stopped", "local", retrieval_error)
        return json.dumps({"error": "retrieval_stopped", "fatal": True, "message": retrieval_error}, ensure_ascii=False)
    if not context.document_ids:
        return json.dumps({"error": "本轮没有选择本地论文", "fatal": True}, ensure_ascii=False)
    required_aspects = {"overview", "problem", "method", "experiments", "conclusion"}
    existing_items = [
        {"section": source.section, "excerpt": source.excerpt}
        for source in context.database.list_sources(context.run_id)
        if source.kind == "local_paper"
    ]
    existing_coverage = evidence_coverage(existing_items)
    if (
        paper_explanation_requested(context.question)
        and required_aspects.issubset(set(existing_coverage["aspects"]))
    ):
        message = "累计证据已覆盖论文的核心问题、方法、实验和结论，无需继续检索。"
        context.database.add_event(
            context.run_id, "evidence_sufficient", "local", message, existing_coverage
        )
        return json.dumps({
            "error": "evidence_sufficient",
            "fatal": True,
            "message": message,
            "coverage": existing_coverage,
        }, ensure_ascii=False)
    normalized_query = context.normalize_query(query)
    if not normalized_query:
        return json.dumps({"error": "检索问题不能为空"}, ensure_ascii=False)
    if normalized_query in context.seen_local_queries:
        message = "该论文检索问题本轮已经执行过。请根据尚缺证据提出不同且更具体的问题。"
        return json.dumps({"error": "duplicate_query", "message": message}, ensure_ascii=False)
    context.seen_local_queries.add(normalized_query)
    if context.database.get_counter(context.run_id, "paperlens_failures") >= 2:
        message = "PaperLens 本轮已连续失败两次，本地检索已熔断。请判断是否改用网页或使用已有证据。"
        context.database.add_event(context.run_id, "circuit_open", "local", message)
        return json.dumps({"error": "paperlens_circuit_open", "fatal": True, "message": message}, ensure_ascii=False)
    context.database.add_event(context.run_id, "tool_start", "local", f"检索本地论文：{query}", {
        "query": query, "search_mode": search_mode,
    })
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
                query.strip(),
                context.document_ids,
                top_k=max(1, min(top_k, 12)),
                reranker_mode="local" if search_mode == "high_quality" else "off",
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
            "fatal": failures >= 2,
            "message": str(exc),
            "consecutive_failures": failures,
        }, ensure_ascii=False)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
    context.database.reset_counter(context.run_id, "paperlens_failures")
    sources = []
    rejected: dict[str, int] = {}
    accepted_items: list[dict] = []
    for item in payload["evidence"]:
        reason = paperlens_rejection_reason(item, query)
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        accepted_items.append(item)
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
    cumulative_items = [
        {"section": source.section, "excerpt": source.excerpt}
        for source in context.database.list_sources(context.run_id)
        if source.kind == "local_paper"
    ]
    coverage = evidence_coverage(cumulative_items)
    explanation_complete = (
        paper_explanation_requested(context.question)
        and required_aspects.issubset(set(coverage["aspects"]))
    )
    if rejected:
        context.database.add_event(
            context.run_id,
            "evidence_filtered",
            "local",
            f"已过滤 {sum(rejected.values())} 条低信息量论文候选",
            {"reasons": rejected, "query": query},
        )
    return json.dumps({
        "query": query,
        "sources": sources,
        "coverage": coverage,
        "coverage_scope": "本轮累计论文证据",
        "sufficient_for_question": explanation_complete,
        "missing_aspects": sorted(required_aspects - set(coverage["aspects"])) if paper_explanation_requested(context.question) else [],
        "filtered_candidates": rejected,
        "instruction": (
            "累计证据已覆盖论文讲解所需关键方面，必须停止检索并返回证据备忘录。"
            if explanation_complete
            else "请根据累计 coverage 和用户问题判断证据是否充分；只为 missing_aspects 中的关键缺口继续检索。"
        ),
    }, ensure_ascii=False)


@tool
async def search_local_papers(
    query: Annotated[str, "针对已选论文的具体检索问题"],
    top_k: Annotated[int, "返回候选证据数量，1 到 12"] = 8,
    search_mode: Annotated[Literal["high_quality", "fast"], "高质量重排或快速混合检索"] = "high_quality",
) -> str:
    """Search selected PaperLens documents and return attributed original passages."""
    context = get_tool_context()
    async with context.local_search_lock:
        return await _search_local_papers_impl(query, top_k, search_mode)


@tool
async def search_web(
    query: Annotated[str, "公开网页搜索关键词或问题"],
    max_results: Annotated[int, "候选网页数量，1 到 8"] = 5,
) -> str:
    """Search the public web without an API key and return candidate URLs."""
    context = get_tool_context()
    permission_error = context.web_permission_error()
    if permission_error:
        return json.dumps({"error": "web_not_permitted", "fatal": True, "message": permission_error}, ensure_ascii=False)
    retrieval_error = context.retrieval_error()
    if retrieval_error:
        context.database.add_event(context.run_id, "retrieval_stopped", "web", retrieval_error)
        return json.dumps({"error": "retrieval_stopped", "fatal": True, "message": retrieval_error}, ensure_ascii=False)
    normalized_query = context.normalize_query(query)
    if normalized_query in context.seen_web_queries:
        return json.dumps({
            "error": "duplicate_query",
            "message": "该网页搜索问题本轮已经执行过。请根据证据缺口决定新查询或停止搜索。",
        }, ensure_ascii=False)
    context.seen_web_queries.add(normalized_query)
    if context.database.get_counter(context.run_id, "web_search_failures") >= 2:
        message = "DDGS 本轮已连续失败两次，网页搜索已熔断。请使用已有证据完成回答。"
        context.database.add_event(context.run_id, "circuit_open", "web", message)
        return json.dumps({"error": "web_search_circuit_open", "fatal": True, "message": message}, ensure_ascii=False)
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
    permission_error = context.web_permission_error()
    if permission_error:
        return json.dumps({"error": "web_not_permitted", "fatal": True, "message": permission_error}, ensure_ascii=False)
    retrieval_error = context.retrieval_error()
    if retrieval_error:
        context.database.add_event(context.run_id, "retrieval_stopped", "web", retrieval_error)
        return json.dumps({"error": "retrieval_stopped", "fatal": True, "message": retrieval_error}, ensure_ascii=False)
    normalized_url = url.strip().lower().rstrip("/")
    if normalized_url in context.seen_web_urls:
        return json.dumps({"error": "duplicate_url", "message": "该网页本轮已经读取过。"}, ensure_ascii=False)
    context.seen_web_urls.add(normalized_url)
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
