import asyncio
import time
from pathlib import Path

from fastapi.testclient import TestClient

from research_agent.api.app import create_app
from research_agent.config import Settings
from research_agent.models import ResearchAnswer


def settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path,
        app_database=tmp_path / "app.sqlite3",
        checkpoint_database=tmp_path / "checkpoints.sqlite3",
        paperlens_base_url="http://127.0.0.1:1",
        paperlens_workspace_id="test",
        paperlens_reranker_mode="off",
        model_base_url="http://model",
        model_api_key="key",
        model_name="fake-model",
        cors_origins=("http://localhost",),
        max_model_calls=20,
        max_web_bytes=10000,
        http_timeout_seconds=0.2,
        ddgs_timeout_seconds=0.2,
        paperlens_timeout_seconds=0.2,
        evidence_timeout_seconds=3,
        run_timeout_seconds=5,
    )


class FakeRuntime:
    available = True

    def __init__(self, _settings, database, _paperlens, _web, _checkpointer) -> None:
        self.database = database

    async def run(self, **kwargs) -> ResearchAnswer:
        run_id = kwargs["run_id"]
        source, _ = self.database.add_source(run_id, {
            "kind": "web", "title": "Fixture", "url": "https://example.com", "excerpt": "Evidence",
        })
        self.database.add_event(run_id, "source_found", "web", "fixture", {"source_id": source.source_id})
        await asyncio.sleep(0)
        return ResearchAnswer(markdown="测试结论 [S1]", citation_ids=["S1"])


def wait_for_terminal(client: TestClient, run_id: str) -> dict:
    for _ in range(100):
        payload = client.get(f"/api/v1/runs/{run_id}").json()
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def test_run_lifecycle_persists_answer_sources_and_sse(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        thread = client.post("/api/v1/threads", json={"title": "API research"}).json()
        response = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"question": "What happened?", "document_ids": [], "use_web": True},
        )
        assert response.status_code == 202
        run = wait_for_terminal(client, response.json()["id"])
        assert run["status"] == "completed"
        assert run["answer"]["citation_ids"] == ["S1"]
        assert client.get(f"/api/v1/runs/{run['id']}/sources").json()[0]["source_id"] == "S1"
        stream = client.get(f"/api/v1/runs/{run['id']}/events")
        assert '"type": "final"' in stream.text
