import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownToLine,
  ArrowLeft,
  BookOpenText,
  Box,
  Check,
  ChevronRight,
  CircleStop,
  Code2,
  FileArchive,
  FileJson,
  FlaskConical,
  Image as ImageIcon,
  LoaderCircle,
  Radio,
  RefreshCw,
  Route,
  ScrollText,
  Upload,
  X,
} from "lucide-react";
import { api, connectEvents } from "./api";
import type { Artifact, CaseDetail, CaseSummary, EventPayload, Phase, PhaseState } from "./types";

const phaseCopy: Record<string, { kicker: string; description: string; icon: typeof BookOpenText }> = {
  paper_analysis: { kicker: "CHAPTER I · READ", description: "把论文拆成可引用的文本、图表与工程事实。", icon: BookOpenText },
  repro_design: { kicker: "CHAPTER II · CHART", description: "将论文主张编排为可以执行和验收的复现任务。", icon: Route },
  project_build: { kicker: "CHAPTER III · BUILD", description: "生成代码、配置、依赖与可审计的工程清单。", icon: Code2 },
  execution: { kicker: "CHAPTER IV · RUN", description: "在安全边界内运行实验，记录失败、修正与部分成果。", icon: FlaskConical },
  evidence_review: { kicker: "CHAPTER V · VERIFY", description: "对照论文证据，汇总风险并生成可交付报告。", icon: ScrollText },
};

const stateText: Record<PhaseState, string> = {
  waiting: "等待启航",
  running: "航行中",
  partial: "已有阶段产物",
  success: "已抵达",
  failed: "遇到阻断",
  cancelled: "已停止",
};

function formatDate(value: string | null | undefined) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function navigate(path: string) {
  history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function App() {
  const [path, setPath] = useState(location.pathname);
  useEffect(() => {
    const sync = () => setPath(location.pathname);
    addEventListener("popstate", sync);
    return () => removeEventListener("popstate", sync);
  }, []);
  const match = path.match(/^\/cases\/([^/]+)$/);
  return match ? <CaseVoyage caseId={match[1]} /> : <CaseLibrary />;
}

function CaseLibrary() {
  const [cases, setCases] = useState<CaseSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [fileName, setFileName] = useState("");

  const load = useCallback(async () => {
    try {
      const result = await api.listCases();
      setCases(result.items);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取案例");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => void load(), [load]);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const form = new FormData(event.currentTarget);
      const result = await api.createCase(form);
      navigate(`/cases/${result.case_id}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "提交失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="library-shell">
      <header className="masthead">
        <div className="wordmark"><span>耿</span>同学 Agent</div>
        <div className="edition">REPRODUCIBILITY LOG · 01</div>
      </header>
      <section className="hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">通信论文工程复现</p>
          <h1>让每一次复现，<br /><em>留下完整航迹。</em></h1>
          <p className="hero-lead">从论文解构到证据审查，实时看见代码如何生成、实验如何修正，以及结论由哪些产物支撑。</p>
          <div className="hero-note"><Radio size={16} /> 当前服务支持多人排队，计算资源受控分配</div>
        </div>
        <form className="departure-card" onSubmit={submit}>
          <div className="card-index">NEW VOYAGE</div>
          <h2>发起一次复现</h2>
          <label className="upload-field">
            <input name="pdf_file" type="file" accept="application/pdf,.pdf" required onChange={(event) => setFileName(event.target.files?.[0]?.name || "")} />
            <Upload size={24} />
            <span>{fileName || "选择或拖入论文 PDF"}</span>
            <small>最大 80 MB · 文件将归档至独立案例目录</small>
          </label>
          <label className="text-label">案例名称（可选）<input name="display_name" maxLength={255} placeholder="例如：WiMAX 自适应调制复现" /></label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="primary-button" disabled={submitting}>{submitting ? <LoaderCircle className="spin" /> : <ChevronRight />} {submitting ? "正在归档…" : "开始航行"}</button>
        </form>
      </section>
      <section className="case-library">
        <div className="section-heading"><div><p className="eyebrow">ARCHIVE</p><h2>案例航海图</h2></div><button className="ghost-button" onClick={() => void load()}><RefreshCw size={16} /> 刷新</button></div>
        {loading ? <div className="skeleton-list"><i /><i /><i /></div> : cases.length === 0 ? (
          <div className="empty-state"><Box /><h3>档案柜还是空的</h3><p>上传第一篇论文，五阶段航线会在这里持续生长。</p></div>
        ) : (
          <div className="case-grid">{cases.map((item, index) => <button key={item.id} className="case-ticket" onClick={() => navigate(`/cases/${item.id}`)}>
            <span className="ticket-number">{String(index + 1).padStart(2, "0")}</span>
            <span className="ticket-main"><strong>{item.display_name}</strong><small>{formatDate(item.created_at)} · {item.source === "import" ? "历史导入" : "网页提交"}</small></span>
            <span className={`job-pill job-${item.job?.status || "idle"}`}>{item.job?.status || "未运行"}</span><ChevronRight size={18} />
          </button>)}</div>
        )}
      </section>
      <footer className="page-footer">本工具评估工程复现风险，不判定论文真伪。</footer>
    </main>
  );
}

function CaseVoyage({ caseId }: { caseId: string }) {
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [events, setEvents] = useState<EventPayload[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState("");
  const [following, setFollowing] = useState(true);
  const [selected, setSelected] = useState<Artifact | null>(null);
  const phaseRefs = useRef<Record<string, HTMLElement | null>>({});

  const refresh = useCallback(async () => {
    try {
      setDetail(await api.getCase(caseId));
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取案例");
    }
  }, [caseId]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!detail?.job || ["succeeded", "failed", "cancelled"].includes(detail.job.status)) return;
    const stream = connectEvents(detail.job.id, (event) => {
      setEvents((current) => [...current.slice(-39), event]);
      void refresh();
    }, setConnected);
    return () => stream.close();
  }, [detail?.job?.id, detail?.job?.status, refresh]);

  const activePhase = detail?.job?.current_phase;
  useEffect(() => {
    if (following && activePhase) phaseRefs.current[activePhase]?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [activePhase, following]);
  useEffect(() => {
    const stopFollowing = () => setFollowing(false);
    addEventListener("wheel", stopFollowing, { passive: true });
    addEventListener("touchstart", stopFollowing, { passive: true });
    return () => { removeEventListener("wheel", stopFollowing); removeEventListener("touchstart", stopFollowing); };
  }, []);

  const artifactsByPhase = useMemo(() => {
    const grouped: Record<string, Artifact[]> = {};
    for (const item of detail?.artifacts || []) (grouped[item.phase] ||= []).push(item);
    return grouped;
  }, [detail?.artifacts]);

  if (!detail) return <main className="loading-page"><LoaderCircle className="spin" /><p>{error || "正在翻开航行日志…"}</p></main>;
  const job = detail.job;
  const completed = detail.phases.filter((phase) => phase.state === "success").length;

  async function cancel() {
    if (job && confirm("将在当前安全边界后停止任务，确定继续吗？")) {
      await api.cancelJob(job.id);
      void refresh();
    }
  }

  return (
    <main className="voyage-shell">
      <header className="voyage-header">
        <button className="back-button" onClick={() => navigate("/")}><ArrowLeft size={17} /> 案例档案</button>
        <div className="voyage-title"><p className="eyebrow">REPRODUCTION VOYAGE</p><h1>{detail.display_name}</h1><p>{formatDate(detail.created_at)} · 航程 {completed}/5</p></div>
        <div className="header-actions">
          <span className={`connection ${connected ? "online" : ""}`}><i />{connected ? "实时连接" : "正在重连"}</span>
          {job && ["queued", "running", "cancel_requested"].includes(job.status) && <button className="danger-ghost" onClick={() => void cancel()}><CircleStop size={16} /> 停止</button>}
          <ExportButton caseId={caseId} />
        </div>
      </header>
      {error && <div className="global-alert" role="alert">{error}</div>}
      <div className="voyage-layout">
        <aside className="route-rail" aria-label="五阶段航线">
          <div className="rail-caption"><Route size={18} /> 航线进度</div>
          <div className="rail-line"><i style={{ height: `${Math.max(0, (completed / 5) * 100)}%` }} /></div>
          {detail.phases.map((phase) => <button key={phase.id} className={`rail-stop state-${phase.state}`} onClick={() => phaseRefs.current[phase.id]?.scrollIntoView({ behavior: "smooth", block: "start" })}>
            <span>{phase.state === "success" ? <Check size={14} /> : phase.index}</span><div><strong>{phase.label}</strong><small>{stateText[phase.state]}</small></div>
          </button>)}
          {!following && activePhase && <button className="follow-button" onClick={() => { setFollowing(true); phaseRefs.current[activePhase]?.scrollIntoView({ behavior: "smooth" }); }}><Radio size={15} /> 返回实时位置</button>}
        </aside>
        <div className="chapters">
          {detail.phases.map((phase) => <PhaseChapter
            key={phase.id}
            phase={phase}
            artifacts={artifactsByPhase[phase.id] || []}
            events={events.filter((event) => event.phase === phase.id)}
            setRef={(node) => { phaseRefs.current[phase.id] = node; }}
            onArtifact={setSelected}
            caseId={caseId}
            jobError={job?.current_phase === phase.id ? job.error : null}
          />)}
        </div>
        <aside className="live-notes">
          <div className="notes-title"><Radio size={15} /> 实时记录</div>
          {events.length === 0 ? <p className="quiet">等待下一条航行记录…</p> : events.slice(-8).reverse().map((event) => <div className="event-note" key={event.id}><time>{formatDate(event.created_at)}</time><p>{event.message || event.type}</p></div>)}
        </aside>
      </div>
      {selected && <ArtifactDrawer artifact={selected} onClose={() => setSelected(null)} />}
    </main>
  );
}

function PhaseChapter({ phase, artifacts, events, setRef, onArtifact, caseId, jobError }: {
  phase: Phase; artifacts: Artifact[]; events: EventPayload[]; setRef: (node: HTMLElement | null) => void;
  onArtifact: (artifact: Artifact) => void; caseId: string; jobError: { code: string; message: string } | null;
}) {
  const copy = phaseCopy[phase.id];
  const Icon = copy.icon;
  return <section ref={setRef} className={`chapter state-${phase.state}`} id={phase.id}>
    <div className="chapter-rule"><span>{String(phase.index).padStart(2, "0")}</span></div>
    <header className="chapter-header">
      <div className="chapter-icon"><Icon /></div><div><p>{copy.kicker}</p><h2>{phase.label}</h2><span>{copy.description}</span></div>
      <div className="chapter-state"><i />{stateText[phase.state]}</div>
    </header>
    <div className="step-strip">{phase.steps.map((step) => <span key={step} className={events.some((event) => event.step === step) ? "observed" : ""}>{step.replaceAll("_", " ")}</span>)}</div>
    {phase.state === "running" && <div className="running-band"><LoaderCircle className="spin" /><div><strong>{events.at(-1)?.message || "智能体正在处理这一章节"}</strong><small>页面可以安全关闭，任务与事件已持久化</small></div></div>}
    {jobError && <details className="error-detail"><summary>查看阻断原因</summary><strong>{jobError.code}</strong><pre>{jobError.message}</pre></details>}
    {artifacts.length > 0 ? <div className="artifact-grid">{artifacts.slice(0, 12).map((artifact) => <ArtifactCard key={artifact.id} artifact={artifact} onOpen={() => onArtifact(artifact)} />)}</div> : <div className="chapter-empty"><span>本章产物将在此归档</span><small>JSON、代码、图像、CSV 与报告都会保留来源和校验摘要。</small></div>}
    {artifacts.length > 0 && <div className="chapter-footer"><span>{artifacts.length} 份产物</span><ExportButton caseId={caseId} phase={phase.id} compact /></div>}
  </section>;
}

function ArtifactCard({ artifact, onOpen }: { artifact: Artifact; onOpen: () => void }) {
  const Icon = artifact.kind === "image" ? ImageIcon : artifact.kind === "json" ? FileJson : artifact.kind === "archive" ? FileArchive : artifact.kind === "code" ? Code2 : ScrollText;
  return <button className="artifact-card" onClick={onOpen}>
    {artifact.kind === "image" ? <img src={artifact.content_url} alt={artifact.path} loading="lazy" /> : <div className="artifact-symbol"><Icon /></div>}
    <span className="artifact-name">{artifact.path.split("/").at(-1)}</span><small>{artifact.path} · {formatSize(artifact.size_bytes)}</small>
  </button>;
}

function ExportButton({ caseId, phase, compact = false }: { caseId: string; phase?: string; compact?: boolean }) {
  const [busy, setBusy] = useState(false);
  async function start() {
    setBusy(true);
    try {
      const created = await api.createExport(caseId, phase);
      let current = await api.getExport(created.export_id);
      while (!["ready", "failed"].includes(current.status)) {
        await new Promise((resolve) => setTimeout(resolve, 700));
        current = await api.getExport(created.export_id);
      }
      if (current.download_url) location.assign(current.download_url);
      else throw new Error(current.error || "导出失败");
    } catch (reason) {
      alert(reason instanceof Error ? reason.message : "导出失败");
    } finally { setBusy(false); }
  }
  return <button className={compact ? "text-button" : "download-button"} disabled={busy} onClick={() => void start()}>{busy ? <LoaderCircle className="spin" size={15} /> : <ArrowDownToLine size={15} />}{compact ? "打包本章" : "下载整案"}</button>;
}

function ArtifactDrawer({ artifact, onClose }: { artifact: Artifact; onClose: () => void }) {
  const [detail, setDetail] = useState<{ preview: { text?: string; rows?: string[][]; json?: unknown } | null } | null>(null);
  useEffect(() => { void api.getArtifact(artifact.id).then(setDetail); }, [artifact.id]);
  return <div className="drawer-scrim" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
    <aside className="artifact-drawer" role="dialog" aria-modal="true" aria-label={`查看产物 ${artifact.path}`}>
      <header><div><p className="eyebrow">ARTIFACT</p><h2>{artifact.path.split("/").at(-1)}</h2><span>{artifact.path} · {formatSize(artifact.size_bytes)}</span></div><button aria-label="关闭" onClick={onClose}><X /></button></header>
      <div className="drawer-body">{artifact.kind === "image" ? <img className="full-image" src={artifact.content_url} alt={artifact.path} /> : !detail ? <LoaderCircle className="spin" /> : detail.preview?.rows ? <div className="table-scroll"><table><tbody>{detail.preview.rows.map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => index === 0 ? <th key={cellIndex}>{cell}</th> : <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table></div> : <pre>{detail.preview?.text || JSON.stringify(detail.preview?.json, null, 2) || "此文件仅支持下载查看。"}</pre>}</div>
      <footer><a className="primary-button" href={artifact.download_url}><ArrowDownToLine size={16} /> 下载原文件</a></footer>
    </aside>
  </div>;
}

export default App;
