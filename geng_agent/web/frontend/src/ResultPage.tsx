import { useEffect, useRef, useState } from "react";
import { ArrowDownToLine, ArrowLeft, BookOpen, ChevronDown, FileText, FolderCode, FolderDown, LoaderCircle, RefreshCw } from "lucide-react";
import { api } from "./api";
import type { CaseResult, PaperCase, ResultReport } from "./types";
import { ErrorNote, formatDate, message } from "./ui";
import "./ResultPage.css";

export function resultHref(id: string) { return `#/results/${encodeURIComponent(id)}`; }

export function resultId(hash: string) {
  const match = /^#\/results\/([^/]+)$/.exec(hash);
  if (!match) return null;
  try { return decodeURIComponent(match[1]); } catch { return null; }
}

export function fileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}

export function ResultContents({ paper, result }: { paper: PaperCase; result: CaseResult }) {
  const [downloading, setDownloading] = useState("");
  const [downloadError, setDownloadError] = useState("");
  const [restarting, setRestarting] = useState(false);
  async function restart() {
    setRestarting(true); setDownloadError("");
    try { await api.retryCase(paper.id); window.location.hash = ""; }
    catch (reason) { setDownloadError(message(reason)); }
    finally { setRestarting(false); }
  }
  async function downloadReport(event: React.MouseEvent<HTMLAnchorElement>, report: ResultReport) {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (downloading) return;
    setDownloading(report.id); setDownloadError("");
    try {
      const blob = await api.downloadReport(paper.id, report.id);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url; link.download = report.name;
      document.body.appendChild(link); link.click(); link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (reason) { setDownloadError(message(reason)); }
    finally { setDownloading(""); }
  }
  return <>
    <section className="result-heading" aria-labelledby="result-title">
      <div className="result-heading-copy"><p className="eyebrow">我的研究成果 / RESEARCH RESULTS</p><h1 id="result-title" tabIndex={-1}>{paper.display_name}</h1>
        <div className="result-meta"><span>提交于 {formatDate(paper.created_at)}</span>{result.finished_at && <span>完成于 {formatDate(result.finished_at)}</span>}{result.available && <span className="result-ready">交付包已就绪</span>}</div>
      </div>
      {result.bundle && <div className="result-bundle"><a className="button primary" href={result.bundle.download_url}><FolderDown size={19} />下载完整交付包</a><span>ZIP · {fileSize(result.bundle.size_bytes)}</span></div>}
    </section>
    {result.message && <p className="result-message" role="status">{result.message}</p>}
    {paper.artifacts_expired_at && paper.can_retry && <div className="result-restart"><p>原论文仍保留，可以重新开始一次复现。</p><button className="button" disabled={restarting} onClick={() => void restart()}>{restarting ? <LoaderCircle size={16} className="spin" /> : <RefreshCw size={16} />}{restarting ? "正在提交…" : "重新复现"}</button></div>}
    {downloadError && <ErrorNote>{downloadError}</ErrorNote>}
    {!!result.reports.length && <section className="result-section" aria-labelledby="result-reports"><div className="result-section-heading"><h2 id="result-reports">{result.reports.length === 2 ? "两份报告" : "报告"}</h2><span>可分别下载，也已收录在交付包中</span></div>
      <div className="result-reports">{result.reports.map(report => <article className="result-report" key={report.id}>
        <span className={`deliverable-icon ${report.id === "comparison" ? "comparison" : "record"}`}><FileText size={24} /></span>
        <div><h3>{report.name.replace(/\.docx$/i, "")}</h3><p>{report.id === "comparison" ? "对照原论文，查看复现结论、结果与差异。" : "了解实验实现、参数假设、运行方法与记录。"}</p><span className="result-file-meta">Word · {fileSize(report.size_bytes)}</span></div>
        <a className="result-report-download" href={report.download_url} onClick={event => void downloadReport(event, report)} aria-disabled={!!downloading} aria-label={`下载${report.name}`} title={`下载${report.name}`}>{downloading === report.id ? <LoaderCircle size={20} className="spin" /> : <ArrowDownToLine size={20} />}<span>{downloading === report.id ? "读取中" : "下载"}</span></a>
      </article>)}</div>
    </section>}
    {!!result.excerpt.length && <section className="result-excerpt result-section" aria-labelledby="result-excerpt-title"><div className="result-section-heading"><h2 id="result-excerpt-title"><BookOpen size={18} />报告导读</h2><span>对比报告原文节选</span></div>
      <div className="result-excerpt-text">{result.excerpt.map((paragraph, index) => <p key={index}>{paragraph}</p>)}</div>
      <p className="result-excerpt-note">这里只展示报告开头的文字，完整结论与对比图表请查看 Word 报告。</p>
    </section>}
    {!!result.tasks.length && <section className="result-section" aria-labelledby="result-tasks"><div className="result-section-heading"><h2 id="result-tasks">复现任务 <span className="result-count">{result.tasks.length}</span></h2><span>每个任务单独收录代码、复现结果与说明</span></div>
      <div className="result-tasks">{result.tasks.map((task, index) => <article className="result-task" key={task.directory}><div className="result-task-heading"><span className="result-task-number">{String(index + 1).padStart(2, "0")}</span><div><h3>{task.name}</h3><p><FolderCode size={14} />代码与配置 {task.code_files} 个<span>·</span>结果文件 {task.result_files} 个</p></div></div>
        {task.readme ? <details className="result-readme"><summary>查看任务说明<ChevronDown size={15} /></summary><pre>{task.readme}</pre></details> : <p className="result-task-note">此任务未附说明文件，代码与已有结果可在交付包中查看。</p>}
      </article>)}</div>
    </section>}
    <p className="result-footnote">交付完成表示报告与文件已整理完毕。各项论文结论是否复现，请以报告中的独立审查意见为准。</p>
  </>;
}

export default function ResultPage({ caseId }: { caseId: string }) {
  const [data, setData] = useState<{ paper: PaperCase; result: CaseResult } | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const page = useRef<HTMLElement>(null);
  useEffect(() => {
    let active = true;
    setData(null); setError("");
    Promise.all([api.getCase(caseId), api.result(caseId)]).then(([paper, result]) => {
      if (active) setData({ paper, result });
    }).catch(reason => { if (active) setError(message(reason)); });
    window.scrollTo(0, 0);
    return () => { active = false; };
  }, [caseId, revision]);
  useEffect(() => {
    if (!data) return;
    const previous = document.title;
    document.title = `${data.paper.display_name} · 复现成果 · 耿同学`;
    page.current?.querySelector<HTMLElement>("h1")?.focus({ preventScroll: true });
    return () => { document.title = previous; };
  }, [data]);
  return <main className="result-page page-width" ref={page}><nav className="result-nav" aria-label="成果页导航"><a className="text-button" href="#"><ArrowLeft size={16} />返回我的论文</a><button className="text-button" onClick={() => setRevision(value => value + 1)}><RefreshCw size={14} />刷新</button></nav>
    {error ? <ErrorNote>{error}<button className="text-button" onClick={() => setRevision(value => value + 1)}>重新加载</button></ErrorNote> : data ? <ResultContents {...data} /> : <div className="empty" role="status"><LoaderCircle size={23} className="spin" /><p>正在读取复现成果</p></div>}
  </main>;
}
