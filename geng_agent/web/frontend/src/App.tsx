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
  ListChecks,
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
  paper_analysis: { kicker: "阶段 1 / 5", description: "提取论文文本、图表、关键参数与工程事实。", icon: BookOpenText },
  repro_design: { kicker: "阶段 2 / 5", description: "将论文主张转化为可执行、可验收的复现任务。", icon: Route },
  task_reproduction: { kicker: "阶段 3 / 5", description: "各任务 writer 编写代码、运行实验并根据论文证据迭代。", icon: Code2 },
  report_composition: { kicker: "阶段 4 / 5", description: "汇总任务核验结果，组织复现说明与论文对比结论。", icon: FlaskConical },
  report_delivery: { kicker: "阶段 5 / 5", description: "生成 Word 报告、审计记录和可下载交付物。", icon: ScrollText },
};

const stateText: Record<PhaseState, string> = {
  waiting: "等待处理",
  running: "处理中",
  partial: "已有阶段产物",
  success: "已完成",
  failed: "处理失败",
  cancelled: "已取消",
};

const stepText: Record<string, string> = {
  start: "初始化案例",
  mineru_layout: "解析版面与图像",
  facts_initial: "抽取全局事实",
  tasks_preliminary: "设计初步任务",
  facts: "定向回补事实",
  tasks: "定稿复现任务",
  thesis: "提炼论文主张",
  experiment_index: "建立实验索引",
  scientific_architecture: "设计科学代码架构",
  foundation: "构建共享科学底座",
  generation: "Writer 复现迭代",
  runtime: "汇总运行证据",
  task_reporters: "逐任务独立核验",
  report_editor: "编排三份报告",
  reports: "生成交付文件",
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
        <div className="wordmark"><span>RP</span>论文复现工作台</div>
        <div className="edition">当前主流程与产物追踪</div>
      </header>
      <section className="hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">论文工程复现系统</p>
          <h1>论文复现流程<br /><em>分阶段可视化</em></h1>
          <p className="hero-lead">从论文解构、复现设计、任务级复现、报告编排到交付物生成，持续展示处理进度、执行记录与阶段产物。</p>
          <div className="hero-note"><Radio size={16} /> 每篇论文独立归档，运行进度与证据持续落盘</div>
        </div>
        <form className="departure-card" onSubmit={submit}>
          <div className="card-index">新建任务</div>
          <h2>提交论文复现</h2>
          <label className="upload-field">
            <input name="pdf_file" type="file" accept="application/pdf,.pdf" required onChange={(event) => setFileName(event.target.files?.[0]?.name || "")} />
            <Upload size={24} />
            <span>{fileName || "选择或拖入论文 PDF"}</span>
            <small>最大 80 MB · 文件将归档至独立案例目录</small>
          </label>
          <label className="text-label">案例名称（可选）<input name="display_name" maxLength={255} placeholder="例如：WiMAX 自适应调制复现" /></label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="primary-button" disabled={submitting}>{submitting ? <LoaderCircle className="spin" /> : <ChevronRight />} {submitting ? "正在创建任务…" : "开始复现"}</button>
        </form>
      </section>
      <section className="case-library">
        <div className="section-heading"><div><p className="eyebrow">案例管理</p><h2>论文复现案例</h2></div><button className="ghost-button" onClick={() => void load()}><RefreshCw size={16} /> 刷新</button></div>
        {loading ? <div className="skeleton-list"><i /><i /><i /></div> : cases.length === 0 ? (
          <div className="empty-state"><Box /><h3>暂无复现案例</h3><p>上传论文后，系统将在此展示五个复现阶段的进度与产物。</p></div>
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
    if (!detail?.job || ["succeeded", "failed", "cancelled"].includes(detail.job.status)) {
      setConnected(false);
      return;
    }
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

  if (!detail) return <main className="loading-page"><LoaderCircle className="spin" /><p>{error || "正在加载复现任务…"}</p></main>;
  const job = detail.job;
  const terminalJob = Boolean(job && ["succeeded", "failed", "cancelled"].includes(job.status));
  const connectionLabel = !job ? "历史案例" : terminalJob ? "任务已结束" : connected ? "实时连接" : "正在连接";
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
        <div className="voyage-title"><p className="eyebrow">论文复现进度</p><h1>{detail.display_name}</h1><p>{formatDate(detail.created_at)} · 已完成 {completed}/5 个阶段</p></div>
        <div className="header-actions">
          <span className={`connection ${connected ? "online" : ""}`}><i />{connectionLabel}</span>
          {job && ["queued", "running", "cancel_requested"].includes(job.status) && <button className="danger-ghost" onClick={() => void cancel()}><CircleStop size={16} /> 停止</button>}
          <ExportButton caseId={caseId} />
        </div>
      </header>
      {error && <div className="global-alert" role="alert">{error}</div>}
      <div className="voyage-layout">
        <aside className="route-rail" aria-label="五阶段复现进度">
          <div className="rail-caption"><ListChecks size={18} /> 阶段进度</div>
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
          <div className="notes-title"><Radio size={15} /> 执行记录</div>
          {events.length === 0 ? <p className="quiet">暂无新的执行记录</p> : events.slice(-8).reverse().map((event) => <div className="event-note" key={event.id}><time>{formatDate(event.created_at)}</time><p>{event.message || event.type}</p></div>)}
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
    <div className="step-strip">{phase.steps.map((step) => <span key={step} className={events.some((event) => event.step === step) ? "observed" : ""}>{stepText[step] || step}</span>)}</div>
    {phase.state === "running" && <div className="running-band"><LoaderCircle className="spin" /><div><strong>{events.at(-1)?.message || "系统正在处理本阶段"}</strong><small>任务状态与执行记录已持久化，可稍后返回查看</small></div></div>}
    {jobError && <details className="error-detail"><summary>查看阻断原因</summary><strong>{jobError.code}</strong><pre>{jobError.message}</pre></details>}
    {artifacts.length > 0 ? <div className="artifact-grid">{artifacts.slice(0, 12).map((artifact) => <ArtifactCard key={artifact.id} artifact={artifact} onOpen={() => onArtifact(artifact)} />)}</div> : <div className="chapter-empty"><span>本阶段暂无产物</span><small>生成的 JSON、代码、图像、CSV 与报告将在此展示。</small></div>}
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
      for (let attempt = 0; attempt < 180 && !["ready", "failed"].includes(current.status); attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 700));
        current = await api.getExport(created.export_id);
      }
      if (current.download_url) location.assign(current.download_url);
      else if (!["ready", "failed"].includes(current.status)) throw new Error("导出仍在后台生成，请稍后重试");
      else throw new Error(current.error || "导出失败");
    } catch (reason) {
      alert(reason instanceof Error ? reason.message : "导出失败");
    } finally { setBusy(false); }
  }
  return <button className={compact ? "text-button" : "download-button"} disabled={busy} onClick={() => void start()}>{busy ? <LoaderCircle className="spin" size={15} /> : <ArrowDownToLine size={15} />}{compact ? "下载本阶段" : "下载全部产物"}</button>;
}

function ArtifactDrawer({ artifact, onClose }: { artifact: Artifact; onClose: () => void }) {
  const [detail, setDetail] = useState<{ preview: { text?: string; rows?: string[][]; json?: unknown } | null } | null>(null);
  const [loadError, setLoadError] = useState("");
  useEffect(() => {
    let active = true;
    setDetail(null);
    setLoadError("");
    void api.getArtifact(artifact.id)
      .then((value) => { if (active) setDetail(value); })
      .catch((reason) => { if (active) setLoadError(reason instanceof Error ? reason.message : "无法读取产物"); });
    return () => { active = false; };
  }, [artifact.id]);
  return <div className="drawer-scrim" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
    <aside className="artifact-drawer" role="dialog" aria-modal="true" aria-label={`查看产物 ${artifact.path}`}>
      <header><div><p className="eyebrow">ARTIFACT</p><h2>{artifact.path.split("/").at(-1)}</h2><span>{artifact.path} · {formatSize(artifact.size_bytes)}</span></div><button aria-label="关闭" onClick={onClose}><X /></button></header>
      <div className="drawer-body">{artifact.kind === "image" ? <img className="full-image" src={artifact.content_url} alt={artifact.path} /> : loadError ? <p className="form-error" role="alert">{loadError}</p> : !detail ? <LoaderCircle className="spin" /> : detail.preview?.rows ? <div className="table-scroll"><table><tbody>{detail.preview.rows.map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => index === 0 ? <th key={cellIndex}>{cell}</th> : <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table></div> : <pre>{detail.preview?.text || JSON.stringify(detail.preview?.json, null, 2) || "此文件仅支持下载查看。"}</pre>}</div>
      <footer><a className="primary-button" href={artifact.download_url}><ArrowDownToLine size={16} /> 下载原文件</a></footer>
    </aside>
  </div>;
}

export default App;
