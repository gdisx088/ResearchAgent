"""Read-only environment and service readiness check."""

from __future__ import annotations

import asyncio
import importlib
import platform

import httpx

from research_agent.config import load_settings
from research_agent.services.paperlens import PaperLensClient


async def main() -> None:
    settings = load_settings()
    print(f"Python: {platform.python_version()}")
    for name in ("deepagents", "fastapi", "langgraph", "ddgs", "trafilatura"):
        module = importlib.import_module(name)
        version = getattr(module, "__version__", "installed")
        print(f"{name}: {version}")
    print(f"Model configured: {settings.model_configured}")
    print(f"Data directory: {settings.data_dir}")
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
        paperlens = PaperLensClient(client, settings.paperlens_base_url, settings.paperlens_workspace_id)
        print(f"PaperLens reachable: {await paperlens.health()}")
    print("DDGS package: available")


if __name__ == "__main__":
    asyncio.run(main())

