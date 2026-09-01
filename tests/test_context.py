import asyncio
import json
import time
from pathlib import Path

import pytest

from research_agent.agent.context import ToolContext, reset_tool_context, set_tool_context
from research_agent.agent.tools import search_local_papers
from research_agent.config import load_settings
from research_agent.db import Database


def _context(tmp_path: Path, *, use_web: bool = True, deadline_offset: float = 60) -> ToolContext:
    settings = load_settings(data_dir=tmp_path)
    settings.ensure_directories()
    database = Database(settings.app_database)
    database.initialize()
    thread = database.create_thread("adaptive research")
    run = database.create_run(thread["id"], "介绍这篇论文", ["paper-1"], use_web)
    return ToolContext(
        run_id=run["id"],
        thread_id=thread["id"],
        question=run["question"],
        document_ids=["paper-1"],
        use_web=use_web,
        evidence_deadline=time.monotonic() + deadline_offset,
        settings=settings,
        database=database,
        paperlens=None,  # type: ignore[arg-type]
        web=None,  # type: ignore[arg-type]
        cancel_event=asyncio.Event(),
    )


def test_web_is_an_agent_permission_not_a_deterministic_route(tmp_path: Path) -> None:
    assert _context(tmp_path, use_web=True).web_permission_error() is None
    assert "未授权" in (_context(tmp_path / "off", use_web=False).web_permission_error() or "")


def test_retrieval_stops_at_time_boundary(tmp_path: Path) -> None:
    context = _context(tmp_path, deadline_offset=-1)
    assert "截止时间" in (context.retrieval_error() or "")


@pytest.mark.asyncio
async def test_parallel_local_delegations_are_serialized_without_count_cap(tmp_path: Path) -> None:
    context = _context(tmp_path)

    class FakePaperLens:
        calls = 0
        active = 0
        max_active = 0

        async def evidence_search(
            self, _query, _document_ids, *, top_k=5, reranker_mode=None
        ):
            call = self.calls
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return {"evidence": [
                {
                    "document_title": "Paper",
                    "document_id": "paper-1",
                    "block_id": f"block-{call}-{index}",
                    "page": call + 1,
                    "section": "Method",
                    "content_type": "text",
                    "excerpt": f"Evidence {call}-{index} contains a complete and useful method description for testing.",
                }
                for index in range(top_k)
            ]}

    paperlens = FakePaperLens()
    context.paperlens = paperlens  # type: ignore[assignment]
    token = set_tool_context(context)
    try:
        await asyncio.gather(
            search_local_papers.ainvoke({"query": "problem", "top_k": 5}),
            search_local_papers.ainvoke({"query": "method", "top_k": 5}),
            search_local_papers.ainvoke({"query": "results", "top_k": 5}),
        )
    finally:
        reset_tool_context(token)

    assert paperlens.calls == 3
    assert paperlens.max_active == 1
    assert context.database.count_sources(context.run_id, "local_paper") == 15


@pytest.mark.asyncio
async def test_explanation_retrieval_stops_on_semantic_coverage_not_count(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context.database.add_source(context.run_id, {
        "kind": "local_paper",
        "title": "Paper",
        "document_id": "paper-1",
        "page": 1,
        "excerpt": (
            "Abstract and introduction describe the adversarial attack problem and motivation. "
            "The proposed method framework optimizes a contrastive loss. Experiments report "
            "results on benchmark datasets and baselines. The conclusion discusses limitations and future work."
        ),
        "metadata": {"content_type": "text"},
    })

    class MustNotRun:
        async def evidence_search(self, *_args, **_kwargs):
            raise AssertionError("PaperLens should not run after semantic coverage is complete")

    context.paperlens = MustNotRun()  # type: ignore[assignment]
    token = set_tool_context(context)
    try:
        result = await search_local_papers.ainvoke({"query": "more details", "top_k": 5})
    finally:
        reset_tool_context(token)
    payload = json.loads(result)
    assert payload["error"] == "evidence_sufficient"
