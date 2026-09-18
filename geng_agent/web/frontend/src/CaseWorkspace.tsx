import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowDownToLine, ArrowLeft, ArrowRight, Check, ChevronRight, CircleStop, Code2, FileText, FolderOpen, LoaderCircle, Radio, RefreshCw, ShieldCheck } from "lucide-react";
import { api, connectEvents } from "./api";
import type { Artifact, CaseDetail, EventPayload } from "./types";
import { engineeringText, formatDate, isActive, jobText, mergeEvents, outcomeLabel, phases, reportSpecs, stateText, stepText } from "./presentation";
import { Badge, Brand, Empty, ErrorNote, Modal, message, navigate } from "./ui";
import { ArtifactBrowser, ArtifactDrawer, ExportButton } from "./Artifacts";

const navigation = [{ id: "overview", label: "流程概览", icon: Radio }, { id: "tasks", label: "任务核验", icon: ShieldCheck }, { id: "reports", label: "中文报告", icon: FileText }, { id: "files", label: "全部产物", icon: FolderOpen }, { id: "activity", label: "执行记录", icon: Code2 }];
const eventLabels: Record<string, string> = { "job.started": "流程开始", "job.finished": "流程结束", "job.failed": "流程中断", "job.cancelled": "流程已停止", "job.retrying": "正在恢复处理", "step.started": "步骤开始", "step.completed": "步骤完成", "phase.started": "阶段开始", "phase.completed": "阶段完成", "artifact.sync_failed": "产物目录等待刷新" };

export function CaseWorkspace({ caseId }: { caseId: string }) {
  const [detail, setDetail] = useState<CaseDetail | null>(null); const [events, setEvents] = useState<EventPayload[]>([]);
  const [connected, setConnected] = useState(false); const [error, setError] = useState("");
  const [selected, setSelected] = useState<Artifact | null>(null); const [action, setAction] = useState<"cancel" | "resume" | null>(null); const [busy, setBusy] = useState(false);
  const latestEvent = useRef(0); const requestVersion = useRef(0); const loadedJob = useRef<string | null>(null);
  const [activeSection, setActiveSection] = useState("overview");
  useEffect(() => {
    if (!detail) return;
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) if (entry.isIntersecting) setActiveSection(entry.target.id);
    }, { rootMargin: "-10% 0px -65% 0px" });
    for (const { id } of navigation) { const section = document.getElementById(id); if (section) observer.observe(section); }
    return () => observer.disconnect();
  }, [Boolean(detail)]);
  const refresh = useCallback(async () => {
    const version = ++requestVersion.current;
    try {
      const data = await api.getCase(caseId); if (version !== requestVersion.current) return;
      const changedJob = loadedJob.current !== (data.job?.id || null); loadedJob.current = data.job?.id || null;
      setDetail(data); setEvents(current => mergeEvents(changedJob ? [] : current, data.recent_events || [])); setError("");
    } catch (reason) { if (version === requestVersion.current) setError(message(reason)); }
  }, [caseId]);
  useEffect(() => { void refresh(); const timer = window.setInterval(() => void refresh(), 5000); return () => { clearInterval(timer); requestVersion.current++; }; }, [refresh]);
  useEffect(() => { latestEvent.current = events.at(-1)?.id || 0; }, [events]);
  const job = detail?.job; const active = isActive(job?.status);
  useEffect(() => {
    if (!job || !active) { setConnected(false); return; }
    const stream = connectEvents(job.id, event => { setEvents(current => mergeEvents(current, [event])); void refresh(); }, setConnected, latestEvent.current);
    return () => stream.close();
  }, [job?.id, active, refresh]);
  async function performAction() {
    setBusy(true);
    try { if (action === "cancel" && job) await api.cancelJob(job.id); else if (action === "resume") { await api.resumeCase(caseId); setEvents([]); latestEvent.current = 0; } setAction(null); await refresh(); }
    catch (reason) { setError(message(reason)); setAction(null); } finally { setBusy(false); }
  }
  if (!detail) return <><header className="topbar"><Brand /></header><main className="page-width loading-page"><button className="button" onClick={() => navigate("/")}><ArrowLeft size={16} /> 返回案例</button>{error ? <ErrorNote>{error}<button className="text-button" onClick={() => void refresh()}>重试</button></ErrorNote> : <div className="loading"><LoaderCircle className="spin" /> 正在读取研究档案</div>}</main></>;
  const research = detail.research; const tasks = research?.tasks || [];
  const completed = detail.phases.filter(phase => phase.state === "success").length;
  const reviewed = tasks.filter(task => task.outcome).length;
  const reportsReady = reportSpecs.filter(spec => detail.artifacts.some(item => item.path === `${spec.stem}.md` || item.path === `${spec.stem}.docx`)).length;
  return <><header className="topbar"><Brand /><button className="button quiet" onClick={() => navigate("/")}><ArrowLeft size={16} /> 全部案例</button></header>
    <div className="workspace page-width"><aside className="workspace-nav"><p className="eyebrow">案例工作区</p>{navigation.map(({ id, label, icon: Icon }) => <a key={id} href={`#${id}`} aria-current={activeSection === id ? "location" : undefined} onClick={() => setActiveSection(id)}><Icon size={17} />{label}</a>)}<div className="nav-note">结论来自独立 Reporter。<br />报告保留原判与不确定性。</div></aside>
      <main className="workspace-main">
        <div className="case-heading"><div><p className="eyebrow">论文复现档案 · {formatDate(detail.created_at)}</p><h1>{detail.display_name}</h1><div className="heading-meta"><Badge value={job?.status || "idle"}>{job ? jobText[job.status] || "状态未知" : "历史案例"}</Badge><span className="subtle">{active ? connected ? "实时更新中" : "定时刷新中 · 实时连接恢复中" : "已保存的案例记录"}</span></div></div><div className="case-actions">{active && job?.status !== "cancel_requested" && <button className="button" onClick={() => setAction("cancel")}><CircleStop size={16} /> 停止</button>}{["failed", "cancelled"].includes(job?.status || "") && <button className="button" onClick={() => setAction("resume")}><RefreshCw size={16} /> 恢复流程</button>}<ExportButton caseId={caseId} /></div></div>
        {error && <ErrorNote>{error}</ErrorNote>}{job?.error && <ErrorNote><strong>流程中断</strong><p>{job.error.message}</p><small>{job.error.code}</small></ErrorNote>}
        <section className="overview panel" id="overview"><div className="section-heading"><div><p className="eyebrow">流程概览</p><h2>{active ? stepText[job?.current_step || ""] || phases[job?.current_phase || ""]?.title || "等待开始处理" : job?.status === "succeeded" ? "流程已结束，请查看逐任务结论" : "当前处理进度"}</h2></div><span className="subtle">{completed} / {detail.phases.length} 阶段完成</span></div>
          <div className="phase-track">{detail.phases.map((phase, i) => <div key={phase.id} className={`phase-item phase-${phase.state}`}><div className="phase-number">{phase.state === "success" ? <Check size={16} /> : phase.state === "running" ? <LoaderCircle className="spin" size={16} /> : String(i + 1).padStart(2, "0")}</div><strong>{phases[phase.id]?.title || phase.label}</strong><small>{stateText[phase.state] || phase.state}</small><p>{phases[phase.id]?.description}</p></div>)}</div>
          <div className="overview-note"><ShieldCheck size={16} /> 阶段完成表示处理结束，论文主张是否得到支持，请以独立核验记录为准。</div>
        </section>
        <section id="tasks" className="section-block"><div className="section-heading"><div><p className="eyebrow">独立 Reporter · 科研结论</p><h2>逐任务核验 <span className="count">{tasks.length}</span></h2></div><span className="subtle">{reviewed} / {tasks.length} 项已有核验记录</span></div>
          {research?.warnings?.map(warning => <ErrorNote key={warning}>{warning}</ErrorNote>)}
          {!tasks.length ? <Empty>实验规划完成后，复现任务会出现在这里。</Empty> : <div className="task-list">{tasks.map((task, i) => <details className="task-card" key={task.task_id}><summary><span className="task-index">{String(i + 1).padStart(2, "0")}</span><span className="task-title"><strong>{task.title}</strong><small>{task.task_id}{task.target && ` · ${task.target}`}</small></span><span className="task-badges"><Badge value={task.outcome || "waiting"}>{outcomeLabel(task.outcome)}</Badge>{task.engineering_status && task.engineering_status !== "verified" && <span className="engineering-warning">{engineeringText[task.engineering_status] || task.engineering_status}</span>}</span><ChevronRight className="disclosure-icon" size={17} /></summary><div className="task-body"><p className="eyebrow">核验理由</p><p>{task.decision_reason || "尚无独立核验结论。任务目标来自实验计划。"}</p>{task.engineering_status && <div className="engineering-note">工程状态：{engineeringText[task.engineering_status] || task.engineering_status}</div>}{task.remaining_uncertainties?.length > 0 && <div className="uncertainties"><strong>仍需核查</strong><ul>{task.remaining_uncertainties.map((item, n) => <li key={n}>{typeof item === "string" ? item : JSON.stringify(item)}</li>)}</ul></div>}<a className="text-button" href="#reports">阅读完整结果对比报告 <ArrowRight size={14} /></a></div></details>)}</div>}
        </section>
        <section id="reports" className="section-block"><div className="section-heading"><div><p className="eyebrow">从结论到证据</p><h2>两份中文报告</h2></div><span className="subtle">{reportsReady} / 2 份报告已有文件</span></div>{research?.editor_ok === false && <ErrorNote>报告编辑尚未完成，已有文件可供查看。科研结论保留在独立核验记录中。</ErrorNote>}
          <div className="report-grid">{reportSpecs.map((spec, i) => { const md = detail.artifacts.find(item => item.path === `${spec.stem}.md`); const docx = detail.artifacts.find(item => item.path === `${spec.stem}.docx`); return <article className={`report-card report-${i}`} key={spec.stem}><div className="report-card-top"><span className="report-icon"><FileText size={23} /></span><span className="mini-tag">{spec.label}</span></div><h3>{spec.title}</h3><p>{spec.description}</p><div className="report-actions"><button className="button primary" disabled={!md} onClick={() => md && setSelected(md)}>{md ? "在线阅读" : "等待报告"}<ArrowRight size={15} /></button>{docx && <a className="text-button" href={docx.download_url}><ArrowDownToLine size={15} /> Word</a>}{md && <a className="text-button" href={md.download_url}>Markdown</a>}</div></article>; })}</div>
          <p className="section-note">报告文件可用不代表论文结论已复现。详细工程记录集中在本地复现报告。</p>
        </section>
        <ArtifactBrowser artifacts={detail.artifacts} onOpen={setSelected} caseId={caseId} />
        <section id="activity" className="section-block"><div className="section-heading"><div><p className="eyebrow">过程记录</p><h2>最近执行记录</h2></div><span className="subtle">包含已保存的历史事件</span></div>{!events.length ? <Empty>此案例暂无已保存的执行事件，可通过产物查看已有结果。</Empty> : <div className="event-list">{events.slice(-20).reverse().map(event => <div className="event-row" key={event.id}><time>{formatDate(event.created_at)}</time><span className="event-dot" /><div><strong>{stepText[event.step || ""] || phases[event.phase || ""]?.title || eventLabels[event.type] || "处理记录"}</strong><p>{event.message || eventLabels[event.type] || event.type}</p></div></div>)}</div>}</section>
        <footer className="page-footer">代码、配置与执行证据保留在交付项目中，可从全部产物下载。</footer>
      </main>
    </div>
    {selected && <ArtifactDrawer key={selected.id} artifact={selected} artifacts={detail.artifacts} onClose={() => setSelected(null)} />}
    {action && <Modal title={action === "cancel" ? "停止当前流程" : "继续处理此案例"} onClose={() => !busy && setAction(null)}><p>{action === "cancel" ? "系统会在当前安全边界后停止，已有结果和执行记录会保留。" : "从已有案例继续处理，复用仍然有效的结果，完成剩余工作。"}</p><div className="modal-actions"><button className="button" disabled={busy} onClick={() => setAction(null)}>返回</button><button className="button primary" disabled={busy} onClick={() => void performAction()}>{busy && <LoaderCircle className="spin" size={16} />}{action === "cancel" ? "确认停止" : "恢复流程"}</button></div></Modal>}
  </>;
}
