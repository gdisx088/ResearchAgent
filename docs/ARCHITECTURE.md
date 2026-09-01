# ResearchAgent V1 Architecture

## Boundary

ResearchAgent owns research sessions, orchestration, public-web retrieval, normalized sources, citations, events, and UI. PaperLens remains the source of truth for local documents, parsing, embeddings, indexes, and page images. The two applications communicate only through versioned HTTP contracts and run in different Conda environments.

## Research lifecycle

1. The API persists a queued run and immediately returns its ID.
2. A background task marks the run active and creates a run-scoped tool context.
3. The main DeepAgent writes a plan and delegates local and/or web evidence acquisition.
4. Every accepted evidence item is normalized, deduplicated, assigned `S<n>`, persisted, and emitted over SSE.
5. A separate critic graph reviews the draft against the actual source bundle.
6. At most one revision is requested. Deterministic validation removes unknown citations and guarantees that exposed citation IDs resolve to persisted sources.
7. The final answer is committed atomically, then emitted to the client.

## Persistence

`research_agent.sqlite3` contains application-owned tables: threads, messages, runs, events, and sources. `checkpoints.sqlite3` is reserved for LangGraph. Keeping them separate prevents checkpoint schema changes from coupling to the public application schema.

SSE event IDs are SQLite primary keys. A reconnecting client can send `Last-Event-ID` or `?after=<id>` to replay only missing events. On startup, queued/running rows left by a previous process are marked `interrupted`; external calls are never resumed automatically.

## Evidence contract

Local evidence retains PaperLens document/block identity, section, page range, bbox metadata, retrieval scores, and an excerpt. Web evidence retains the final URL after validated redirects, extracted title, text excerpt, content type, and retrieval context. Agent-visible citations are generated only after the source is persisted.

## Failure behavior

- PaperLens failure disables local search for that tool call but does not stop web research.
- PaperLens evidence calls are serialized globally. The first failure opens a run-scoped circuit breaker, so a timed-out synchronous inference request cannot trigger a retry storm while it is still executing server-side.
- Agent retrieval defaults to PaperLens `reranker_mode=off`; hybrid Dense/BM25/RRF evidence remains available without loading the additional CrossEncoder. The independent PaperLens timeout covers the measured E5 cold start.
- DDGS or page-fetch failure is emitted as a tool error and does not discard local evidence.
- Missing model configuration keeps management APIs available and reports the model capability as unavailable; a submitted run fails with a specific configuration error.
- Cancellation sets a cooperative flag and cancels the asyncio task. Synchronous DDGS work already running in its worker thread may finish in the background, but its result is not committed to the cancelled run.
