from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from research_agent.agent.runtime import AgentRuntime, question_requires_external_web
from research_agent.config import load_settings
from research_agent.db import Database
from research_agent.services.paperlens import PaperLensClient
from research_agent.services.web import WebResearchService


@pytest.mark.asyncio
async def test_deepagents_runtime_compiles_with_sqlite_checkpointer(tmp_path: Path) -> None:
    settings = replace(
        load_settings(data_dir=tmp_path),
        model_base_url="https://example.invalid/v1",
        model_api_key="test-key",
        model_name="test-model",
    )
    database = Database(settings.app_database)
    database.initialize()
    async with httpx.AsyncClient() as client:
        paperlens = PaperLensClient(client, settings.paperlens_base_url, settings.paperlens_workspace_id)
        web = WebResearchService(client, max_bytes=1000, timeout_seconds=1)
        async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_database)) as checkpointer:
            await checkpointer.setup()
            runtime = AgentRuntime(settings, database, paperlens, web, checkpointer)
            assert runtime.available is True
            assert runtime.main_agent is not None
            assert runtime.critic_agent is not None


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("介绍这篇论文，以及它解决了领域中的什么问题", False),
        ("结合最新研究进展评价这篇论文", True),
        ("Compare with other recent studies", True),
        ("总结论文的实验结果", False),
    ],
)
def test_external_web_routing(question: str, expected: bool) -> None:
    assert question_requires_external_web(question) is expected
