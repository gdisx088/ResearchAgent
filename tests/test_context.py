import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest

from research_agent.agent.context import ToolContext, reset_tool_context, set_tool_context
from research_agent.agent.tools import search_local_papers
from research_agent.config import load_settings
from research_agent.db import Database


def _context(tmp_path: Path, *, external_web_required: bool = False) -> ToolContext:
    settings = load_settings(data_dir=tmp_path)
    database = Database(settings.app_database)
    database.initialize()
    thread = database.create_thread("routing")
    run = database.create_run(thread["id"], "介绍这篇论文", ["paper-1"], True)
    return ToolContext(
        run_id=run["id"],
        thread_id=thread["id"],
        question=run["question"],
        document_ids=["paper-1"],
        use_web=True,
        external_web_required=external_web_required,
        evidence_deadline=time.monotonic() + 60,
        settings=settings,
        database=database,
        paperlens=None,  # type: ignore[arg-type]
        web=None,  # type: ignore[arg-type]
        cancel_event=asyncio.Event(),
    )


def test_optional_web_is_blocked_before_local_search(tmp_path: Path) -> None:
    context = _context(tmp_path)
    assert "禁止并行网页检索" in (context.web_route_error() or "")


def test_optional_web_is_blocked_after_local_evidence(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context.database.add_source(context.run_id, {
        "kind": "local_paper",
        "title": "Paper",
        "document_id": "paper-1",
        "page": 1,
        "excerpt": "Evidence",
    })
    assert "已取得本地论文证据" in (context.web_route_error() or "")


def test_explicit_external_request_allows_web(tmp_path: Path) -> None:
    context = _context(tmp_path, external_web_required=True)
    assert context.web_route_error() is None


@pytest.mark.asyncio
async def test_parallel_local_delegations_stop_after_source_cap(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context.settings = replace(context.settings, max_local_sources=5)

    class FakePaperLens:
        calls = 0

        async def evidence_search(self, _query, _document_ids, top_k=5):
            self.calls += 1
            await asyncio.sleep(0.01)
            return {"evidence": [
                {
                    "document_title": "Paper",
                    "document_id": "paper-1",
                    "block_id": f"block-{index}",
                    "page": index + 1,
                    "excerpt": f"Evidence {index}",
                }
                for index in range(top_k)
            ]}

    paperlens = FakePaperLens()
    context.paperlens = paperlens  # type: ignore[assignment]
    token = set_tool_context(context)
    try:
        await asyncio.gather(
            search_local_papers.ainvoke({"query": "problem", "top_k": 5}),
            search_local_papers.ainvoke({"query": "contribution", "top_k": 5}),
            search_local_papers.ainvoke({"query": "results", "top_k": 5}),
        )
    finally:
        reset_tool_context(token)

    assert paperlens.calls == 1
    assert context.database.count_sources(context.run_id, "local_paper") == 5
