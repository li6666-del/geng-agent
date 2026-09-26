import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowDownToLine, ArrowRight, BookOpen, Check, FileText, FolderCode, FolderDown, LoaderCircle, LogOut, Plus, RefreshCw, Search, Upload, X } from "lucide-react";
import { api } from "./api";
import type { PaperCase, SiteConfig, User } from "./types";
import { Brand, ErrorNote, formatDate, isActive, message, statusText } from "./ui";
import { SignalArtwork } from "./SignalArtwork";
import FormulaBackdrop from "./FormulaBackdrop";
import researcherIllustration from "./assets/telecom-researcher-v1.png";
import satelliteIllustration from "./assets/telecom-satellite-v1.png";
import { WelcomeNotice } from "./WelcomeNotice";

// Bump the notice version when its content materially changes.
const WELCOME_NOTICE_KEY = "geng-agent:welcome-notice:2026-09-26";

function shouldShowWelcomeNotice() {
  try { return localStorage.getItem(WELCOME_NOTICE_KEY) !== "read"; }
  catch { return true; }
}

export function DeliveryContents() {
  return <div className="delivery-contents">
    <div><span className="deliverable-icon comparison"><FileText size={20} /></span><span><strong>论文复现结果对比报告</strong><small>原文与复现结果的对照、结论与说明</small></span><span className="file-tag">Word</span></div>
    <div><span className="deliverable-icon record"><FileText size={20} /></span><span><strong>本地复现报告</strong><small>实现方法、运行环境与复现记录</small></span><span className="file-tag">Word</span></div>
    <div><span className="deliverable-icon code"><FolderCode size={20} /></span><span><strong>分任务复现项目</strong><small>代码、配置、运行说明与结果文件</small></span><span className="file-tag">代码</span></div>
  </div>;
}

export function AuthPage({ site, onLogin }: { site: SiteConfig; onLogin: (user: User) => void }) {
  const [register, setRegister] = useState(false);
  const [email, setEmail] = useState(""); const [password, setPassword] = useState(""); const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault(); setError("");
    if (register && password !== confirmation) { setError("两次输入的密码不一致。"); return; }
    setBusy(true);
    try { onLogin(await api.authenticate(register ? "register" : "login", email, password)); }
    catch (reason) { setError(message(reason)); } finally { setBusy(false); }
  }
  return <main className="auth-page page-width"><div className="auth-layout"><section className="auth-intro"><p className="research-label"><span /> BUPT GENG AGENT <i /> 为通信研究而造</p><h1>从通信论文，<br />到<span className="accent-heading">可复现的实验。</span></h1><p className="intro-copy">让公式走进代码，让结论回到实验。<br />上传一篇论文，交给智能体复现与独立核验。</p>
    <div className="hero-lab" aria-hidden="true"><div className="lab-orbit" /><img className="researcher-illustration" src={researcherIllustration} width={1536} height={1024} alt="" fetchPriority="high" /><span className="lab-note"><span /> 灵感，正在连接</span><span className="lab-coordinate">PAPER → CODE → DISCOVERY</span></div>
    <div className="intro-foot"><span><Check size={14} /> 独立核验结论</span><span><Check size={14} /> 保留假设与差距</span><span><Check size={14} /> 代码按任务交付</span></div>
  </section><section className="auth-card" aria-labelledby="auth-title"><div className="auth-card-top"><span className="auth-signal" aria-hidden="true"><i /><i /><i /><i /><i /></span><span>RESEARCH SPACE</span></div><h2 id="auth-title">{register ? "创建账号" : "欢迎回来"}</h2><p className="subtle">{register ? "开启你的第一篇通信论文复现。" : "登录你的研究空间，继续探索。"}</p><form onSubmit={event => void submit(event)}>
    <label>邮箱<input type="email" autoComplete="email" required maxLength={254} value={email} onChange={event => setEmail(event.target.value)} placeholder="你的邮箱地址" disabled={busy} /></label>
    <label>密码<input type="password" autoComplete={register ? "new-password" : "current-password"} required minLength={10} maxLength={128} value={password} onChange={event => setPassword(event.target.value)} placeholder="至少 10 个字符" disabled={busy} /></label>
    {register && <label>确认密码<input type="password" autoComplete="new-password" required minLength={10} maxLength={128} value={confirmation} onChange={event => setConfirmation(event.target.value)} placeholder="再次输入密码" disabled={busy} /></label>}
    {error && <ErrorNote>{error}</ErrorNote>}<button className="button primary wide" disabled={busy}>{busy ? <LoaderCircle size={17} className="spin" /> : <ArrowRight size={17} />}{busy ? "请稍候…" : register ? "注册并进入" : "登录"}</button>
  </form>{site.registration_enabled && <p className="auth-switch">{register ? "已有账号？" : "还没有账号？"}<button className="text-button" disabled={busy} onClick={() => { setRegister(!register); setError(""); setPassword(""); setConfirmation(""); }}>{register ? "去登录" : "创建账号"}</button></p>}<div className="auth-card-foot"><FolderDown size={14} /> 论文、实验与成果，在这里相连</div></section></div>
    <section className="outcome-strip" aria-labelledby="outcome-title"><div className="outcome-heading"><div><p className="eyebrow">从探索，到交付</p><h2 id="outcome-title">一篇论文，一份完整的研究成果</h2></div><span className="bundle-label"><FolderDown size={15} /> 一次下载 · ZIP 交付包</span></div><DeliveryContents /></section><footer className="site-footer"><span>BUPT · 耿同学</span><span>专注通信论文复现，让研究有迹可循。</span></footer></main>;
}

function UploadCard({ site, onCreated }: { site: SiteConfig; onCreated: () => Promise<void> }) {
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null); const [name, setName] = useState("");
  const [busy, setBusy] = useState(false); const [dragging, setDragging] = useState(false); const [error, setError] = useState("");
  function select(candidate?: File) {
    if (!candidate) return;
    if (!candidate.name.toLowerCase().endsWith(".pdf")) { setError("请选择 PDF 格式的论文。"); return; }
    if (candidate.size > site.max_pdf_bytes) { setError(`文件大小不能超过 ${Math.floor(site.max_pdf_bytes / 1048576)} MB。`); return; }
    setFile(candidate); setError("");
  }
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!file) { setError("请先选择论文 PDF。"); return; }
    setBusy(true); setError("");
    try {
      const form = new FormData(); form.set("pdf_file", file); form.set("display_name", name.trim());
      await api.createCase(form); setFile(null); setName(""); await onCreated();
    } catch (reason) { setError(message(reason)); } finally { setBusy(false); }
  }
  return <section className="upload-panel"><div className="section-title"><span className="title-icon"><Plus size={18} /></span><h2>提交一篇论文</h2><span className="section-caption">新的研究，从这里开始</span></div><form onSubmit={event => void submit(event)}>
    <button type="button" disabled={busy} className={`dropzone ${dragging ? "dragging" : ""} ${file ? "has-file" : ""}`} onClick={() => input.current?.click()} onDragOver={event => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); if (!busy) select(event.dataTransfer.files[0]); }}>
      <span className="upload-symbol">{file ? <FileText size={25} /> : <Upload size={25} />}</span><strong>{file ? file.name : "点击选择，或将论文拖到这里"}</strong><small>{file ? `${(file.size / 1048576).toFixed(1)} MB · 点击可更换` : `PDF 格式 · 最大 ${Math.floor(site.max_pdf_bytes / 1048576)} MB`}</small>
    </button><input ref={input} type="file" accept="application/pdf,.pdf" hidden onChange={event => { select(event.target.files?.[0]); event.target.value = ""; }} />
    <div className="upload-bottom"><label>论文名称 <span className="optional">选填</span><input value={name} maxLength={255} disabled={busy} onChange={event => setName(event.target.value)} placeholder="默认使用文件名" /></label><button className="button primary" disabled={busy}>{busy ? <LoaderCircle size={17} className="spin" /> : <ArrowRight size={17} />}{busy ? "正在提交…" : "开始复现"}</button></div>{error && <ErrorNote>{error}</ErrorNote>}
  </form></section>;
}

export function CaseRow({ item, busy, onAction }: { item: PaperCase; busy: boolean; onAction: (item: PaperCase, cancel: boolean) => void }) {
  return <article className="case-row"><span className="paper-icon"><FileText size={21} /></span><div className="case-description"><h3>{item.display_name}</h3><p>{formatDate(item.created_at)}<span>·</span>{item.message}</p></div><span className={`status status-${item.status}`}><i />{statusText[item.status] || "待确认"}</span><div className="row-action">
    {item.download_url ? <a className="button download" href={item.download_url}><ArrowDownToLine size={16} />下载交付包</a> : item.can_retry ? <button className="button" disabled={busy} onClick={() => onAction(item, false)}>{busy ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}继续处理</button> : isActive(item.status) && item.status !== "cancel_requested" ? <button className="text-button muted" disabled={busy} onClick={() => onAction(item, true)}>停止</button> : <span className="subtle">—</span>}
  </div></article>;
}

function Dashboard({ site }: { site: SiteConfig }) {
  const [cases, setCases] = useState<PaperCase[]>([]); const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const [query, setQuery] = useState(""); const [busyId, setBusyId] = useState("");
  const load = useCallback(async () => {
    try { setCases((await api.listCases()).items); setLoaded(true); setError(""); } catch (reason) { setError(message(reason)); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  const active = cases.some(item => isActive(item.status));
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 15000);
    const visible = () => { if (!document.hidden) void load(); };
    document.addEventListener("visibilitychange", visible);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", visible); };
  }, [active, load]);
  async function action(item: PaperCase, cancel: boolean) {
    if (cancel && !window.confirm("停止这篇论文的复现？已有运行记录会保留，可以稍后继续。")) return;
    setBusyId(item.id);
    try { if (cancel) await api.cancelCase(item.id); else await api.retryCase(item.id); await load(); }
    catch (reason) { setError(message(reason)); } finally { setBusyId(""); }
  }
  const visible = cases.filter(item => item.display_name.toLowerCase().includes(query.toLowerCase()));
  return <main className="dashboard page-width"><section className="workspace-banner"><div className="dashboard-heading"><p className="eyebrow">我的研究空间 <span>/ COMMUNICATION LAB</span></p><h1>从一篇论文开始。</h1><p className="subtle">把通信研究的灵感，变成可运行、可核查的实验。</p><div className="research-topics"><span>通信论文</span><i /><span>实验复现</span><i /><span>结果核验</span></div></div><div className="workspace-art" aria-hidden="true"><div className="workspace-signal"><SignalArtwork compact /></div><img className="satellite-illustration" src={satelliteIllustration} width={1280} height={1280} alt="" /></div></section>
    <div className="submission-grid"><UploadCard site={site} onCreated={async () => { setNotice("论文已提交。你可以关闭页面，稍后登录查看结果。"); await load(); }} /><aside className="delivery-panel"><div className="delivery-heading"><div><p className="eyebrow">完成后，你将获得</p><h2>一份完整交付包</h2></div><span className="package-mark" aria-hidden="true"><FolderDown size={25} /></span></div><DeliveryContents /><p className="delivery-note"><FolderDown size={15} /> 两份报告与复现项目，打包为一个 ZIP 文件</p></aside></div>
    <section className="cases-section"><div className="cases-heading"><h2>我的论文 <span>{cases.length}</span></h2><div className="list-tools"><label className="search"><Search size={16} /><input aria-label="搜索我的论文" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索论文名称" /></label><button className="icon-button" aria-label="刷新任务列表" onClick={() => void load()}><RefreshCw size={17} /></button></div></div>
      {notice && <div className="notice" role="status"><Check size={17} /><span>{notice}</span><button className="icon-button" aria-label="关闭提示" onClick={() => setNotice("")}><X size={16} /></button></div>}{error && <ErrorNote>{error}</ErrorNote>}
      {!loaded && !error ? <div className="empty"><LoaderCircle className="spin" size={22} /><p>正在读取你的论文</p></div> : !visible.length ? <div className="empty"><span className="empty-paper"><FileText size={27} /></span><h3>{cases.length ? "没有找到匹配的论文" : "你的第一份复现成果，从这里开始"}</h3><p>{cases.length ? "换个关键词试试。" : "提交上方的论文，完成后即可下载报告与代码。"}</p></div> : <div className="case-list">{visible.map(item => <CaseRow key={item.id} item={item} busy={busyId === item.id} onAction={(entry, cancel) => void action(entry, cancel)} />)}</div>}
    </section><footer className="site-footer"><span>BUPT · 耿同学</span><span>报告如实记录复现结论、假设和差异。处理完成不代表所有结论均已复现。</span></footer></main>;
}

export default function App() {
  const [noticeOpen, setNoticeOpen] = useState(shouldShowWelcomeNotice);
  const [user, setUser] = useState<User | null>(null); const [site, setSite] = useState<SiteConfig | null>(null);
  const [ready, setReady] = useState(false); const [error, setError] = useState(""); const [loggingOut, setLoggingOut] = useState(false);
  const initialize = useCallback(async () => {
    setError("");
    try { const [config, account] = await Promise.all([api.site(), api.session()]); setSite(config); setUser(account); setReady(true); }
    catch (reason) { setError(message(reason)); }
  }, []);
  useEffect(() => { void initialize(); const expired = () => setUser(null); window.addEventListener("session-expired", expired); return () => window.removeEventListener("session-expired", expired); }, [initialize]);
  async function logout() { setLoggingOut(true); try { await api.logout(); setUser(null); setError(""); } catch (reason) { setError(message(reason)); } finally { setLoggingOut(false); } }
  function acknowledgeNotice() {
    try { localStorage.setItem(WELCOME_NOTICE_KEY, "read"); } catch { /* Reading the notice does not require browser storage. */ }
    setNoticeOpen(false);
  }
  return <div className="site-shell"><FormulaBackdrop /><header className="topbar"><div className="page-width header-inner"><Brand /><div className="header-actions"><button type="button" className="notice-trigger" onClick={() => setNoticeOpen(true)}><BookOpen size={15} />使用说明</button>{user ? <div className="account"><span className="account-avatar" aria-hidden="true">{user.email.slice(0, 1).toUpperCase()}</span><span className="account-email">{user.email}</span><button className="text-button logout-button" aria-label="退出登录" title="退出登录" disabled={loggingOut} onClick={() => void logout()}><LogOut size={16} /><span>退出登录</span></button></div> : <span className="header-note"><span /> 通信论文复现与独立核验</span>}</div></div></header>
    {error && <div className="page-width app-error"><ErrorNote>{error}<button className="text-button" onClick={() => void initialize()}>重新连接</button></ErrorNote></div>}
    {!ready || !site ? !error && <main className="empty"><LoaderCircle size={25} className="spin" /><p>正在打开研究空间</p></main> : user ? <Dashboard key={user.id} site={site} /> : <AuthPage site={site} onLogin={setUser} />}
    <WelcomeNotice open={noticeOpen} onDismiss={() => setNoticeOpen(false)} onAcknowledge={acknowledgeNotice} />
  </div>;
}
