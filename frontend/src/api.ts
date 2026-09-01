import type { Capabilities, Paper, ResearchEvent, Run, SourceRecord, ThreadDetail, ThreadSummary } from "./types";

async function jsonRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof body === "object" && body && "detail" in body ? String(body.detail) : String(body);
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return body as T;
}

export const api = {
  capabilities: () => jsonRequest<Capabilities>("/api/v1/capabilities"),
  listThreads: () => jsonRequest<ThreadSummary[]>("/api/v1/threads"),
  createThread: (title = "新研究") => jsonRequest<ThreadSummary>("/api/v1/threads", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title })
  }),
  getThread: (id: string) => jsonRequest<ThreadDetail>(`/api/v1/threads/${id}`),
  createRun: (threadId: string, question: string, documentIds: string[], useWeb: boolean) =>
    jsonRequest<Run>(`/api/v1/threads/${threadId}/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, document_ids: documentIds, use_web: useWeb })
    }),
  cancelRun: (id: string) => jsonRequest(`/api/v1/runs/${id}/cancel`, { method: "POST" }),
  getSources: (id: string) => jsonRequest<SourceRecord[]>(`/api/v1/runs/${id}/sources`),
  listPapers: () => jsonRequest<Paper[]>("/api/v1/papers"),
  uploadPaper: async (file: File): Promise<{ job_id: string }> => {
    const form = new FormData();
    form.append("file", file);
    return jsonRequest("/api/v1/papers", { method: "POST", body: form });
  },
  updatePaper: (id: string, payload: { display_name?: string; enabled?: boolean }) =>
    jsonRequest<Paper>(`/api/v1/papers/${id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
    }),
  reindexPaper: (id: string) => jsonRequest<{ job_id: string }>(`/api/v1/papers/${id}/reindex`, { method: "POST" }),
  deletePaper: (id: string) => jsonRequest<void>(`/api/v1/papers/${id}`, { method: "DELETE" })
};

export function subscribeRun(runId: string, onEvent: (event: ResearchEvent) => void, onError: () => void): EventSource {
  const stream = new EventSource(`/api/v1/runs/${runId}/events`);
  stream.onmessage = (message) => onEvent(JSON.parse(message.data) as ResearchEvent);
  stream.onerror = onError;
  return stream;
}

export function subscribePaperJob(jobId: string, onDone: () => void, onStatus: (message: string) => void): EventSource {
  const stream = new EventSource(`/api/v1/paper-jobs/${jobId}/events`);
  stream.addEventListener("status", (event) => {
    const payload = JSON.parse((event as MessageEvent).data);
    onStatus(payload.message || "正在索引论文");
  });
  stream.addEventListener("final", () => { stream.close(); onDone(); });
  stream.addEventListener("error", () => { stream.close(); onStatus("论文索引失败"); });
  return stream;
}

