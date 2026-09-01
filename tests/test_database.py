from pathlib import Path

from research_agent.db import Database
from research_agent.models import ResearchAnswer


def test_database_persists_threads_runs_events_and_sources(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    thread = database.create_thread("测试研究")
    run = database.create_run(thread["id"], "比较两种方法", ["doc-a"], True)
    database.add_event(run["id"], "queued", "system", "queued")
    first, created = database.add_source(run["id"], {
        "kind": "local_paper", "title": "Paper A", "document_id": "doc-a",
        "block_id": "b1", "page": 3, "excerpt": "Evidence",
    })
    duplicate, duplicate_created = database.add_source(run["id"], {
        "kind": "local_paper", "title": "Paper A", "document_id": "doc-a",
        "block_id": "b1", "page": 3, "excerpt": "Evidence",
    })
    answer = ResearchAnswer(markdown="结论 [S1]", citation_ids=["S1"])
    database.set_run_status(run["id"], "completed", answer=answer)

    assert created is True
    assert duplicate_created is False
    assert duplicate.source_id == first.source_id == "S1"
    assert database.get_run(run["id"])["answer"]["citation_ids"] == ["S1"]
    assert database.list_events(run["id"])[0]["type"] == "queued"
    assert database.list_sources(run["id"])[0].page == 3


def test_restart_marks_active_runs_interrupted(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    thread = database.create_thread("restart")
    run = database.create_run(thread["id"], "question", [], False)
    database.set_run_status(run["id"], "running")

    assert database.mark_incomplete_interrupted() == 1
    assert database.get_run(run["id"])["status"] == "interrupted"
    assert database.list_events(run["id"])[-1]["type"] == "interrupted"


def test_tool_budget_is_atomic_and_never_exceeds_limit(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    thread = database.create_thread("budget")
    run = database.create_run(thread["id"], "question", [], True)

    assert [database.claim_budget(run["id"], "论文检索", 2) for _ in range(3)] == [
        (True, 1), (True, 2), (False, 2)
    ]
