import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, subscribePaperJob, subscribeRun } from "./api";
import type { Capabilities, Paper, ResearchAnswer, ResearchEvent, SourceRecord, ThreadDetail, ThreadSummary } from "./types";

const terminalEvents = new Set(["final", "error", "cancelled", "interrupted"]);

function capabilityLabel(available: boolean, label: string) {
  return <span className={`capability ${available ? "online" : "offline"}`}><i />{label}</span>;
}

function sourceMarkdown(markdown: string) {
  return markdown.replace(/\[(S\d+)\](?!\()/g, "[$1](source:$1)");
}

function MarkdownAnswer({ markdown, sources, onSource }: {
  markdown: string;
  sources: SourceRecord[];
  onSource: (sourceId: string) => void;
}) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} urlTransform={(url) => url} components={{
    a: ({ href, children }) => href?.startsWith("source:")
      ? <button className="citation" onClick={() => onSource(href.slice(7))}>{children}</button>
      : <a href={href} target="_blank" rel="noreferrer">{children}</a>
  }}>{sourceMarkdown(markdown)}</ReactMarkdown>;
}

export default function App() {
  const streamRef = useRef<EventSource | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [activeThread, setActiveThread] = useState<ThreadDetail | null>(null);
  const [papers, setPapers] = useState<Paper[]>([]);
  const [selectedPapers, setSelectedPapers] = useState<Set<string>>(new Set());
  const [useWeb, setUseWeb] = useState(true);
  const [question, setQuestion] = useState("");
  const [events, setEvents] = useState<ResearchEvent[]>([]);
  const [sources, setSources] = useState<SourceRecord[]>([]);
  const [answer, setAnswer] = useState<ResearchAnswer | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [selectedSource, setSelectedSource] = useState<SourceRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  const refreshThreads = useCallback(async () => setThreads(await api.listThreads()), []);
  const refreshPapers = useCallback(async () => {
    try {
      const values = await api.listPapers();
      setPapers(values);
      setSelectedPapers((current) => new Set([...current].filter((id) => values.some((paper) => paper.id === id))));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "无法读取 PaperLens 论文库");
    }
  }, []);

  useEffect(() => {
    Promise.allSettled([api.capabilities(), api.listThreads(), api.listPapers()]).then(([caps, threadList, paperList]) => {
      if (caps.status === "fulfilled") setCapabilities(caps.value);
      if (threadList.status === "fulfilled") setThreads(threadList.value);
      if (paperList.status === "fulfilled") setPapers(paperList.value);
    });
    return () => streamRef.current?.close();
  }, []);

  const openThread = useCallback(async (id: string) => {
    streamRef.current?.close();
    setActiveRunId(null);
    setEvents([]);
    setSources([]);
    setSelectedSource(null);
    const detail = await api.getThread(id);
    setActiveThread(detail);
    const completed = [...detail.runs].reverse().find((run) => run.answer);
    setAnswer(completed?.answer || null);
    if (completed) setSources(await api.getSources(completed.id));
  }, []);

  async function ensureThread(): Promise<ThreadDetail> {
    if (activeThread) return activeThread;
    const created = await api.createThread(question.trim().slice(0, 36) || "新研究");
    await refreshThreads();
    const detail = await api.getThread(created.id);
    setActiveThread(detail);
    return detail;
  }

  async function submit() {
    const clean = question.trim();
    if (!clean || busy) return;
    setBusy(true);
    setNotice("");
    setEvents([]);
    setSources([]);
    setAnswer(null);
    try {
      const thread = await ensureThread();
      const run = await api.createRun(thread.id, clean, [...selectedPapers], useWeb);
      setQuestion("");
      setActiveRunId(run.id);
      const optimistic: ThreadDetail = {
        ...thread,
        messages: [...(thread.messages || []), {
          id: `pending-${run.id}`, role: "user", content: clean, run_id: run.id, metadata: {}, created_at: new Date().toISOString()
        }],
        runs: [...(thread.runs || []), run]
      };
      setActiveThread(optimistic);
      streamRef.current?.close();
      streamRef.current = subscribeRun(run.id, async (event) => {
        setEvents((current) => [...current.filter((item) => item.id !== event.id), event].sort((a, b) => a.id - b.id));
        if (event.type === "source_found") setSources(await api.getSources(run.id));
        if (event.type === "final" && event.data.answer) setAnswer(event.data.answer);
        if (terminalEvents.has(event.type)) {
          streamRef.current?.close();
          setBusy(false);
          setActiveRunId(null);
          setActiveThread(await api.getThread(thread.id));
          setSources(await api.getSources(run.id));
          await refreshThreads();
        }
      }, () => {
        if (streamRef.current?.readyState === EventSource.CLOSED) setBusy(false);
      });
    } catch (error) {
      setBusy(false);
      setNotice(error instanceof Error ? error.message : "任务启动失败");
    }
  }

  async function cancel() {
    if (!activeRunId) return;
    try {
      await api.cancelRun(activeRunId);
      setNotice("正在取消任务…");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "取消失败");
    }
  }

  async function upload(file?: File) {
    if (!file) return;
    setNotice(`正在上传 ${file.name}…`);
    try {
      const job = await api.uploadPaper(file);
      subscribePaperJob(job.job_id, async () => {
        setNotice(`${file.name} 已完成索引`);
        await refreshPapers();
      }, setNotice);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "上传失败");
    }
  }

  async function renamePaper(paper: Paper) {
    const name = window.prompt("新的论文显示名称", paper.display_name || paper.file_name)?.trim();
    if (!name) return;
    await api.updatePaper(paper.id, { display_name: name });
    await refreshPapers();
  }

  async function removePaper(paper: Paper) {
    if (!window.confirm(`永久删除“${paper.display_name || paper.file_name}”及其索引？`)) return;
    await api.deletePaper(paper.id);
    await refreshPapers();
  }

  async function openSource(sourceId: string, runId?: string) {
    let available = sources;
    if (runId && !available.some((source) => source.run_id === runId && source.source_id === sourceId)) {
      available = await api.getSources(runId);
      setSources(available);
    }
    setSelectedSource(available.find((source) => source.source_id === sourceId && (!runId || source.run_id === runId)) || null);
  }

  const currentMessages = activeThread?.messages || [];
  const activePaperCount = useMemo(() => selectedPapers.size, [selectedPapers]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">R</span><div><strong>ResearchAgent</strong><small>深度研搜工作台</small></div></div>
        <div className="capabilities">
          {capabilityLabel(Boolean(capabilities?.model.available), capabilities?.model.model || "回答模型")}
          {capabilityLabel(Boolean(capabilities?.paperlens.available), "PaperLens")}
          {capabilityLabel(Boolean(capabilities?.web.available), "DDGS Web")}
          {capabilityLabel(true, "SQLite")}
        </div>
      </header>

      <main className="workspace">
        <aside className="sidebar sessions">
          <div className="section-heading"><span>研究会话</span><button onClick={() => { setActiveThread(null); setAnswer(null); setEvents([]); setSources([]); }}>＋</button></div>
          <div className="thread-list">
            {threads.map((thread) => (
              <button className={activeThread?.id === thread.id ? "thread active" : "thread"} key={thread.id} onClick={() => openThread(thread.id)}>
                <strong>{thread.title}</strong><small>{thread.message_count || 0} 条消息</small>
              </button>
            ))}
            {!threads.length && <p className="empty">从一个研究问题开始</p>}
          </div>
          <div className="paper-header"><span>本地论文</span><label className="upload">上传<input type="file" accept=".pdf,.md,.txt" onChange={(e) => upload(e.target.files?.[0])} /></label></div>
          <div className="paper-list">
            {papers.map((paper) => (
              <div className={`paper ${paper.enabled && paper.status === "ready" ? "" : "disabled"}`} key={paper.id}>
                <label>
                  <input type="checkbox" checked={selectedPapers.has(paper.id)} disabled={!paper.enabled || paper.status !== "ready"}
                    onChange={() => setSelectedPapers((current) => {
                      const next = new Set(current); next.has(paper.id) ? next.delete(paper.id) : next.add(paper.id); return next;
                    })} />
                  <span><strong>{paper.display_name || paper.file_name}</strong><small>{paper.status}</small></span>
                </label>
                <div className="paper-actions">
                  <button title="重命名" onClick={() => renamePaper(paper)}>✎</button>
                  <button title={paper.enabled ? "停用" : "启用"} onClick={async () => { await api.updatePaper(paper.id, { enabled: !paper.enabled }); await refreshPapers(); }}>{paper.enabled ? "○" : "●"}</button>
                  <button title="重建索引" onClick={async () => { const job = await api.reindexPaper(paper.id); subscribePaperJob(job.job_id, refreshPapers, setNotice); }}>↻</button>
                  <button title="永久删除" onClick={() => removePaper(paper)}>×</button>
                </div>
              </div>
            ))}
            {!papers.length && <p className="empty">连接 PaperLens 后可上传论文</p>}
          </div>
        </aside>

        <section className="research-panel">
          <div className="conversation">
            {!currentMessages.length && !answer && (
              <div className="hero"><span>RESEARCH / EVIDENCE / SYNTHESIS</span><h1>把一个问题，变成<br />可核验的研究结论。</h1><p>选择本地论文，决定是否检索公开网页，然后交给多 Agent 团队完成证据搜集、审查与回答。</p></div>
            )}
            {currentMessages.map((message) => (
              <article className={`message ${message.role}`} key={message.id}>
                <div className="message-label">{message.role === "user" ? "YOU" : "RESEARCH AGENT"}</div>
                {message.role === "assistant" ? <MarkdownAnswer markdown={message.content} sources={sources} onSource={(sourceId) => openSource(sourceId, message.run_id)} /> : <p>{message.content}</p>}
              </article>
            ))}
            {answer && !currentMessages.some((message) => message.role === "assistant" && message.content === answer.markdown) && (
              <article className="message assistant"><div className="message-label">RESEARCH AGENT</div>
                <MarkdownAnswer markdown={answer.markdown} sources={sources} onSource={(sourceId) => openSource(sourceId, activeRunId || undefined)} />
                {answer.limitations.length > 0 && <div className="limitations"><strong>局限</strong>{answer.limitations.map((item) => <p key={item}>{item}</p>)}</div>}
              </article>
            )}
          </div>
          <div className="composer-wrap">
            {notice && <div className="notice">{notice}<button onClick={() => setNotice("")}>×</button></div>}
            <div className="scope-row"><span>{activePaperCount} 篇本地论文</span><label><input type="checkbox" checked={useWeb} onChange={(e) => setUseWeb(e.target.checked)} />允许补充公开网页</label></div>
            <div className="composer"><textarea value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="输入需要深度研究的问题…"
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }} />
              {busy ? <button className="stop" onClick={cancel}>停止</button> : <button className="send" disabled={!question.trim()} onClick={submit}>开始研究 →</button>}
            </div>
          </div>
        </section>

        <aside className="sidebar evidence">
          <div className="section-heading"><span>研究轨迹</span><small>{events.length}</small></div>
          <div className="event-list">
            {events.map((event) => <div className={`event event-${event.type}`} key={event.id}><i /><div><strong>{event.message}</strong><small>{event.stage} · {new Date(event.created_at).toLocaleTimeString()}</small></div></div>)}
            {!events.length && <p className="empty">任务开始后在这里查看规划、检索和审查过程</p>}
          </div>
          <div className="source-heading">证据来源 <span>{sources.length}</span></div>
          <div className="source-list">
            {sources.map((source) => <button key={source.source_id} className={selectedSource?.source_id === source.source_id ? "source active" : "source"} onClick={() => setSelectedSource(source)}>
              <b>{source.source_id}</b><span><strong>{source.title}</strong><small>{source.kind === "local_paper" ? `${source.section || "论文"}${source.page ? ` · P${source.page}` : ""}` : "公开网页"}</small></span>
            </button>)}
          </div>
          {selectedSource && <div className="source-detail"><button className="close" onClick={() => setSelectedSource(null)}>×</button><h3>{selectedSource.source_id} · {selectedSource.title}</h3>
            {selectedSource.kind === "web" && selectedSource.url && <a href={selectedSource.url} target="_blank" rel="noreferrer">打开原始网页 ↗</a>}
            {selectedSource.kind === "local_paper" && selectedSource.document_id && selectedSource.page && <img alt={`${selectedSource.title} 第${selectedSource.page}页`} src={`/api/v1/papers/${selectedSource.document_id}/pages/${selectedSource.page}`} />}
            <p>{selectedSource.excerpt}</p></div>}
        </aside>
      </main>
    </div>
  );
}
