import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowRight, BookOpenText, ChevronRight, LoaderCircle, RefreshCw, Search, ShieldCheck, Upload } from "lucide-react";
import { api } from "./api";
import type { CaseSummary } from "./types";
import { formatDate, formatSize, isActive, jobText, phases } from "./presentation";
import { Badge, Brand, Empty, ErrorNote, message, navigate } from "./ui";
import { CaseWorkspace } from "./CaseWorkspace";

function App() {
  const [path, setPath] = useState(location.pathname);
  useEffect(() => { const sync = () => setPath(location.pathname); window.addEventListener("popstate", sync); return () => window.removeEventListener("popstate", sync); }, []);
  const match = path.match(/^\/cases\/([^/]+)$/);
  return match ? <CaseWorkspace key={match[1]} caseId={match[1]} /> : <CaseLibrary />;
}

function CaseLibrary() {
  const [cases, setCases] = useState<CaseSummary[]>([]);
  const [loading, setLoading] = useState(true); const [error, setError] = useState("");
  const [query, setQuery] = useState(""); const [filter, setFilter] = useState("all");
  const load = useCallback(async () => {
    try { setCases((await api.listCases()).items); setError(""); }
    catch (reason) { setError(message(reason)); } finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); const timer = window.setInterval(() => void load(), 10000); return () => clearInterval(timer); }, [load]);
  const active = cases.filter(item => isActive(item.job?.status)).length;
  const visible = cases.filter(item => item.display_name.toLowerCase().includes(query.toLowerCase()) && (filter === "all" || (filter === "active" ? isActive(item.job?.status) : ["failed", "cancelled"].includes(item.job?.status || ""))));
  return <><header className="topbar"><Brand /><span className="top-note"><span className="status-dot" /> 本地研究工作区</span></header>
    <main className="library page-width"><div className="lab-heading"><div><h1>通信论文复现</h1><p>上传论文，跟踪实验，查阅独立核验结果。</p></div><div className="lab-metrics"><div><span>案例总数</span><strong>{loading ? "—" : cases.length}</strong></div><div><span>正在处理</span><strong>{loading ? "—" : active}</strong></div></div></div><section className="intro-grid">
      <div className="intro"><p className="eyebrow">工作流程</p><h2>从论文主张，<br />到可核查的实验结果。</h2><p className="intro-description">解析论文并组织复现实验，通过独立核验，汇总为结果对比与本地复现两份中文报告。</p>
        <div className="workflow-mini">{Object.entries(phases).map(([id, phase], i) => <span key={id}><b>0{i + 1}</b>{phase.title}{i < 4 && <ChevronRight size={12} />}</span>)}</div>
        <div className="principle"><ShieldCheck size={18} /><span>事实、假设与实验结果分别记录，保留差距和不确定性。</span></div>
      </div><UploadCard />
    </section>
    <section className="case-section" aria-labelledby="case-heading">
      <div className="section-heading"><div><p className="eyebrow">研究档案</p><h2 id="case-heading">我的复现案例 <span className="count">{cases.length}</span></h2></div><span className="subtle">{active ? `${active} 个案例正在处理` : "案例、执行证据与报告持续保存"}</span></div>
      <div className="toolbar"><div className="filter-tabs" aria-label="案例筛选">{[["all", "全部案例"], ["active", "处理中"], ["attention", "已中断 / 已停止"]].map(([id, label]) => <button key={id} aria-pressed={filter === id} className={filter === id ? "selected" : ""} onClick={() => setFilter(id)}>{label}</button>)}</div><div className="toolbar-right"><label className="search"><Search size={16} /><input aria-label="搜索案例" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索论文或案例…" /></label><button className="icon-button" aria-label="刷新案例" onClick={() => void load()}><RefreshCw size={17} /></button></div></div>
      {error && <ErrorNote>{error}</ErrorNote>}
      {loading ? <div className="loading"><LoaderCircle className="spin" /> 正在读取案例</div> : visible.length === 0 ? <Empty>{cases.length ? "没有符合筛选条件的案例。" : "还没有案例。上传第一篇论文，开始建立复现档案。"}</Empty> : <div className="case-list"><div className="case-table-heading"><span>论文 / 案例</span><span>当前阶段</span><span>处理状态</span></div>{visible.map(item => <button className="case-row" key={item.id} onClick={() => navigate(`/cases/${item.id}`)}>
        <span className="paper-icon"><BookOpenText size={22} /></span><span className="case-name"><strong>{item.display_name}</strong><small>{formatDate(item.created_at)}<span>·</span>{item.source === "import" ? "本地导入" : item.source === "url" ? "链接导入" : "PDF 上传"}</small></span><span className="case-phase">{isActive(item.job?.status) ? phases[item.job?.current_phase || ""]?.title || "等待开始" : "查看任务与报告"}</span><Badge value={item.job?.status || "idle"}>{item.job ? jobText[item.job.status] || "状态未知" : "历史案例"}</Badge><ChevronRight size={18} />
      </button>)}</div>}
    </section><footer className="page-footer">流程结束与科研结论分别记录。复现结果保留失败、假设和不确定性。</footer></main></>;
}

function UploadCard() {
  const [file, setFile] = useState<File | null>(null); const [name, setName] = useState("");
  const [dragging, setDragging] = useState(false); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const [maxBytes, setMaxBytes] = useState<number | null>(null); const input = useRef<HTMLInputElement>(null);
  useEffect(() => { void api.health().then(value => setMaxBytes(value.max_pdf_bytes)).catch(() => {}); }, []);
  function select(selected?: File) {
    if (!selected) return;
    if (!selected.name.toLowerCase().endsWith(".pdf")) { setError("请选择 PDF 格式的论文。"); return; }
    if (maxBytes && selected.size > maxBytes) { setError(`论文不能超过 ${formatSize(maxBytes)}。`); return; }
    setFile(selected); setError("");
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault(); if (!file) { setError("请先选择论文 PDF。"); return; }
    setBusy(true); setError("");
    try { const form = new FormData(); form.set("pdf_file", file); if (name.trim()) form.set("display_name", name.trim()); const created = await api.createCase(form); navigate(`/cases/${created.case_id}`); }
    catch (reason) { setError(message(reason)); } finally { setBusy(false); }
  }
  return <form className="upload-card" onSubmit={event => void submit(event)}><div className="card-heading"><span className="eyebrow">新建复现</span><span className="mini-tag">PDF</span></div><h2>从一篇论文开始</h2><p className="subtle">提交后开始解析与复现，可随时返回查看。</p>
    <button type="button" disabled={busy} className={`dropzone ${dragging ? "dragging" : ""}`} onClick={() => input.current?.click()} onDragOver={event => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); if (!busy) select(event.dataTransfer.files[0]); }}><span className="upload-symbol"><Upload size={23} /></span><strong>{file ? file.name : "点击选择或拖入论文"}</strong><small>{file ? formatSize(file.size) : `PDF 文件${maxBytes ? ` · 最大 ${formatSize(maxBytes)}` : ""}`}</small></button>
    <input ref={input} type="file" accept="application/pdf,.pdf" hidden onChange={event => { select(event.target.files?.[0]); event.target.value = ""; }} />
    <label className="field-label">案例名称 <span>可选</span><input value={name} maxLength={255} disabled={busy} onChange={event => setName(event.target.value)} placeholder="例如：瑞利信道 BER 曲线复现" /></label>
    {error && <ErrorNote>{error}</ErrorNote>}<button className="button primary wide" disabled={busy}>{busy ? <LoaderCircle className="spin" size={17} /> : <ArrowRight size={17} />}{busy ? "正在提交…" : "开始论文复现"}</button>
  </form>;
}

export default App;
