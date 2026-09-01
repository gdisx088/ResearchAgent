"""FastAPI application for durable research tasks and PaperLens proxying."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from research_agent.agent.runtime import AgentRuntime
from research_agent.config import Settings, load_settings
from research_agent.db import Database, TERMINAL_STATUSES
from research_agent.models import PaperUpdate, ResearchAnswer, ResearchRunRequest, ThreadCreate
from research_agent.services.paperlens import PaperLensClient, PaperLensError
from research_agent.services.web import WebResearchService


RuntimeFactory = Callable[[Settings, Database, PaperLensClient, WebResearchService, Any], Any]


class ApplicationServices:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        paperlens: PaperLensClient,
        web: WebResearchService,
        runtime: Any,
    ) -> None:
        self.settings = settings
        self.database = database
        self.paperlens = paperlens
        self.web = web
        self.runtime = runtime
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}

    async def execute(self, run_id: str) -> None:
        run = self.database.get_run(run_id)
        cancel_event = self.cancel_events[run_id]
        self.database.set_run_status(run_id, "running")
        self.database.add_event(run_id, "run_started", "system", "研究任务已开始")
        try:
            async with asyncio.timeout(self.settings.run_timeout_seconds):
                answer: ResearchAnswer = await self.runtime.run(
                    run_id=run_id,
                    thread_id=run["thread_id"],
                    question=run["question"],
                    document_ids=run["document_ids"],
                    use_web=run["use_web"],
                    cancel_event=cancel_event,
                )
            if cancel_event.is_set():
                raise asyncio.CancelledError
            self.database.set_run_status(run_id, "completed", answer=answer)
            self.database.add_message(
                run["thread_id"],
                "assistant",
                answer.markdown,
                run_id=run_id,
                metadata={"citation_ids": answer.citation_ids, "limitations": answer.limitations},
            )
            self.database.add_event(
                run_id, "final", "complete", "研究回答已完成", {"answer": answer.model_dump()}
            )
        except asyncio.CancelledError:
            cancel_event.set()
            self.database.set_run_status(run_id, "cancelled")
            self.database.add_event(run_id, "cancelled", "system", "任务已取消")
        except TimeoutError:
            message = f"研究任务超过 {int(self.settings.run_timeout_seconds)} 秒，已停止以避免无限检索"
            self.database.set_run_status(run_id, "failed", error=message)
            self.database.add_event(run_id, "error", "system", message, {"error_type": "RunTimeout"})
        except Exception as exc:
            self.database.set_run_status(run_id, "failed", error=str(exc))
            self.database.add_event(
                run_id,
                "error",
                "system",
                f"研究任务失败：{exc}",
                {"error_type": type(exc).__name__},
            )
        finally:
            self.tasks.pop(run_id, None)
            self.cancel_events.pop(run_id, None)

    def start(self, run_id: str) -> None:
        cancel_event = asyncio.Event()
        self.cancel_events[run_id] = cancel_event
        task = asyncio.create_task(self.execute(run_id))
        self.tasks[run_id] = task

    async def cancel(self, run_id: str) -> bool:
        task = self.tasks.get(run_id)
        event = self.cancel_events.get(run_id)
        if task is None or task.done() or event is None:
            return False
        event.set()
        task.cancel()
        return True


def create_app(
    settings: Settings | None = None,
    *,
    runtime_factory: RuntimeFactory | None = None,
) -> FastAPI:
    app_settings = settings or load_settings()
    factory = runtime_factory or AgentRuntime

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app_settings.ensure_directories()
        database = Database(app_settings.app_database)
        database.initialize()
        database.mark_incomplete_interrupted()
        timeout = httpx.Timeout(app_settings.http_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            paperlens = PaperLensClient(
                client,
                app_settings.paperlens_base_url,
                app_settings.paperlens_workspace_id,
                app_settings.paperlens_timeout_seconds,
                app_settings.paperlens_reranker_mode,
            )
            web = WebResearchService(
                client,
                max_bytes=app_settings.max_web_bytes,
                timeout_seconds=app_settings.http_timeout_seconds,
                search_timeout_seconds=app_settings.ddgs_timeout_seconds,
            )
            async with AsyncSqliteSaver.from_conn_string(str(app_settings.checkpoint_database)) as checkpointer:
                await checkpointer.setup()
                runtime = factory(app_settings, database, paperlens, web, checkpointer)
                app.state.services = ApplicationServices(app_settings, database, paperlens, web, runtime)
                yield
                services: ApplicationServices = app.state.services
                for event in services.cancel_events.values():
                    event.set()
                for task in services.tasks.values():
                    task.cancel()
                if services.tasks:
                    await asyncio.gather(*services.tasks.values(), return_exceptions=True)

    app = FastAPI(title="ResearchAgent API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(app_settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def services(request: Request) -> ApplicationServices:
        return request.app.state.services

    @app.get("/api/v1/health")
    async def health(request: Request) -> dict[str, str]:
        _ = services(request)
        return {"status": "ok"}

    @app.get("/api/v1/capabilities")
    async def capabilities(request: Request) -> dict[str, Any]:
        state = services(request)
        paperlens_available = await state.paperlens.health()
        return {
            "model": {"available": bool(state.runtime.available), "model": state.settings.model_name},
            "paperlens": {
                "available": paperlens_available,
                "base_url": state.settings.paperlens_base_url,
                "workspace_id": state.settings.paperlens_workspace_id,
                "reranker_mode": state.settings.paperlens_reranker_mode,
            },
            "web": {"available": state.web.search_available, "provider": "DDGS"},
            "persistence": {"available": True},
            "limits": {
                "local_searches": state.settings.max_local_searches,
                "web_searches": state.settings.max_web_searches,
                "web_fetches": state.settings.max_web_fetches,
                "local_sources": state.settings.max_local_sources,
                "evidence_timeout_seconds": state.settings.evidence_timeout_seconds,
                "run_timeout_seconds": state.settings.run_timeout_seconds,
            },
        }

    @app.get("/api/v1/threads")
    async def list_threads(request: Request) -> list[dict[str, Any]]:
        return services(request).database.list_threads()

    @app.post("/api/v1/threads", status_code=201)
    async def create_thread(body: ThreadCreate, request: Request) -> dict[str, Any]:
        return services(request).database.create_thread(body.title)

    @app.get("/api/v1/threads/{thread_id}")
    async def get_thread(thread_id: str, request: Request) -> dict[str, Any]:
        try:
            return services(request).database.get_thread(thread_id)
        except KeyError as exc:
            raise HTTPException(404, "研究会话不存在") from exc

    @app.post("/api/v1/threads/{thread_id}/runs", status_code=202)
    async def create_run(thread_id: str, body: ResearchRunRequest, request: Request) -> dict[str, Any]:
        state = services(request)
        try:
            state.database.get_thread(thread_id, include_detail=False)
            run = state.database.create_run(
                thread_id, body.question.strip(), list(dict.fromkeys(body.document_ids)), body.use_web
            )
        except KeyError as exc:
            raise HTTPException(404, "研究会话不存在") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        state.database.add_event(run["id"], "queued", "system", "研究任务已进入队列")
        state.start(run["id"])
        return run

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> dict[str, Any]:
        try:
            return services(request).database.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(404, "研究任务不存在") from exc

    @app.get("/api/v1/runs/{run_id}/sources")
    async def run_sources(run_id: str, request: Request) -> list[dict[str, Any]]:
        state = services(request)
        try:
            state.database.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(404, "研究任务不存在") from exc
        return [source.model_dump() for source in state.database.list_sources(run_id)]

    @app.post("/api/v1/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
        state = services(request)
        try:
            run = state.database.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(404, "研究任务不存在") from exc
        if run["status"] in TERMINAL_STATUSES or not await state.cancel(run_id):
            raise HTTPException(409, "任务已经结束或不在当前进程中运行")
        return {"run_id": run_id, "status": "cancelling"}

    @app.get("/api/v1/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        after: int = 0,
    ) -> StreamingResponse:
        state = services(request)
        try:
            state.database.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(404, "研究任务不存在") from exc
        cursor = max(after, int(last_event_id or 0))

        async def generate() -> AsyncIterator[str]:
            nonlocal cursor
            heartbeat = 0
            while True:
                if await request.is_disconnected():
                    break
                rows = state.database.list_events(run_id, cursor)
                for event in rows:
                    cursor = event["id"]
                    yield f"id: {event['id']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                run = state.database.get_run(run_id)
                if run["status"] in TERMINAL_STATUSES and not rows:
                    break
                heartbeat += 1
                if heartbeat % 15 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.2)

        return StreamingResponse(generate(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    def paperlens_failure(exc: PaperLensError) -> HTTPException:
        return HTTPException(503, str(exc))

    @app.get("/api/v1/papers")
    async def papers(request: Request) -> list[dict[str, Any]]:
        try:
            return await services(request).paperlens.list_documents()
        except PaperLensError as exc:
            raise paperlens_failure(exc) from exc

    @app.post("/api/v1/papers", status_code=202)
    async def upload_paper(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
        content = await file.read()
        if not content:
            raise HTTPException(400, "上传文件为空")
        try:
            return await services(request).paperlens.upload_document(
                file.filename or "paper.pdf", content, file.content_type
            )
        except PaperLensError as exc:
            raise paperlens_failure(exc) from exc

    @app.get("/api/v1/paper-jobs/{job_id}/events")
    async def paper_job_events(job_id: str, request: Request) -> StreamingResponse:
        async def proxy() -> AsyncIterator[bytes]:
            try:
                async for chunk in services(request).paperlens.stream_job_events(job_id):
                    yield chunk
            except PaperLensError as exc:
                payload = json.dumps({"message": str(exc)}, ensure_ascii=False)
                yield f"event: error\ndata: {payload}\n\n".encode()
        return StreamingResponse(proxy(), media_type="text/event-stream")

    @app.patch("/api/v1/papers/{document_id}")
    async def update_paper(document_id: str, body: PaperUpdate, request: Request) -> dict[str, Any]:
        payload = body.model_dump(exclude_none=True)
        if not payload:
            raise HTTPException(400, "没有需要更新的字段")
        try:
            return await services(request).paperlens.update_document(document_id, payload)
        except PaperLensError as exc:
            raise paperlens_failure(exc) from exc

    @app.delete("/api/v1/papers/{document_id}", status_code=204)
    async def delete_paper(document_id: str, request: Request) -> Response:
        try:
            await services(request).paperlens.delete_document(document_id)
        except PaperLensError as exc:
            raise paperlens_failure(exc) from exc
        return Response(status_code=204)

    @app.post("/api/v1/papers/{document_id}/reindex", status_code=202)
    async def reindex_paper(document_id: str, request: Request) -> dict[str, Any]:
        try:
            return await services(request).paperlens.reindex_document(document_id)
        except PaperLensError as exc:
            raise paperlens_failure(exc) from exc

    @app.get("/api/v1/papers/{document_id}/pages/{page}")
    async def paper_page(document_id: str, page: int, request: Request) -> Response:
        try:
            content, content_type = await services(request).paperlens.page_preview(document_id, page)
        except PaperLensError as exc:
            raise paperlens_failure(exc) from exc
        return Response(content, media_type=content_type)

    return app


app = create_app()
