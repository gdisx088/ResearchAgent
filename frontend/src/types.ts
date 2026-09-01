export type RunStatus = "queued" | "running" | "completed" | "failed" | "cancelled" | "interrupted";

export interface Capabilities {
  model: { available: boolean; model: string };
  paperlens: { available: boolean; base_url: string; workspace_id: string };
  web: { available: boolean; provider: string };
  persistence: { available: boolean };
}

export interface ThreadSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count?: number;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  run_id?: string;
  metadata: { citation_ids?: string[]; limitations?: string[] };
  created_at: string;
}

export interface ResearchAnswer {
  markdown: string;
  citation_ids: string[];
  limitations: string[];
}

export interface Run {
  id: string;
  thread_id: string;
  question: string;
  document_ids: string[];
  use_web: boolean;
  status: RunStatus;
  answer: ResearchAnswer | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface ThreadDetail extends ThreadSummary {
  messages: Message[];
  runs: Run[];
}

export interface ResearchEvent {
  id: number;
  run_id: string;
  type: string;
  stage: string;
  message: string;
  data: Record<string, unknown> & { answer?: ResearchAnswer };
  created_at: string;
}

export interface SourceRecord {
  source_id: string;
  run_id: string;
  kind: "local_paper" | "web";
  title: string;
  url?: string;
  document_id?: string;
  block_id?: string;
  page?: number;
  page_end?: number;
  section?: string;
  excerpt: string;
  status: string;
  metadata: Record<string, unknown>;
}

export interface Paper {
  id: string;
  file_name: string;
  display_name: string;
  enabled: boolean;
  status: string;
  title?: string;
  chunk_count?: number;
}

