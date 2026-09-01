# ResearchAgent V1 Architecture

## Boundary

ResearchAgent owns research sessions, orchestration, public-web retrieval, normalized sources, citations, events, and UI. PaperLens remains the source of truth for local documents, parsing, embeddings, indexes, and page images. The two applications communicate only through versioned HTTP contracts and run in different Conda environments.

## Research lifecycle

1. The API persists a queued run and immediately returns its ID.
2. A background task marks the run active and creates a run-scoped tool context.
3. The main DeepAgent writes a plan, chooses local and/or web sources, and delegates evidence acquisition without fixed per-tool call quotas.
4. Low-information PaperLens hits are filtered; every accepted item is normalized, deduplicated, assigned `S<n>`, persisted, and emitted over SSE.
5. A coverage graph judges whether the current evidence answers the actual question. Missing semantic aspects are returned to the orchestrator for another agent-decided research pass; convergence, duplicate work, cancellation, and time budgets stop the loop.
6. A tool-free writer graph builds the user-facing answer from a high-signal source bundle. Research notes and revision instructions never serve as the final response.
7. A separate critic graph reviews the answer against the actual source bundle. At most one writer revision is requested.
8. Presentation cleanup and deterministic validation remove orchestration leakage, normalize citation syntax, and guarantee that exposed citation IDs resolve to persisted sources.
9. The final answer is committed atomically, then emitted to the client.

## Persistence

`research_agent.sqlite3` contains application-owned tables: threads, messages, runs, events, and sources. `checkpoints.sqlite3` is reserved for LangGraph. Keeping them separate prevents checkpoint schema changes from coupling to the public application schema.

SSE event IDs are SQLite primary keys. A reconnecting client can send `Last-Event-ID` or `?after=<id>` to replay only missing events. On startup, queued/running rows left by a previous process are marked `interrupted`; external calls are never resumed automatically.

## Evidence contract

Local evidence retains PaperLens document/block identity, section, page range, bbox metadata, retrieval scores, and an excerpt. Web evidence retains the final URL after validated redirects, extracted title, text excerpt, content type, and retrieval context. Agent-visible citations are generated only after the source is persisted.

## Failure behavior

- PaperLens failure disables local search for that tool call but does not stop web research.
- PaperLens evidence calls are serialized globally. Two consecutive failures open a run-scoped circuit breaker, preventing a timed-out synchronous inference request from creating a retry storm while still tolerating one transient failure.
- Agent retrieval defaults to PaperLens `reranker_mode=local` for CrossEncoder-backed quality. A specialist may select fast hybrid Dense/BM25/RRF retrieval for exploration. The independent PaperLens timeout covers embedding and reranker cold starts.
- Paper, web-search, and page-fetch tools have no normal fixed call quotas. Exact duplicate queries/URLs, semantic coverage, circuit breakers, cancellation, evidence deadlines, and a high model-call safety ceiling bound execution.
- DDGS or page-fetch failure is emitted as a tool error and does not discard local evidence.
- Missing model configuration keeps management APIs available and reports the model capability as unavailable; a submitted run fails with a specific configuration error.
- Cancellation sets a cooperative flag and cancels the asyncio task. Synchronous DDGS work already running in its worker thread may finish in the background, but its result is not committed to the cancelled run.
