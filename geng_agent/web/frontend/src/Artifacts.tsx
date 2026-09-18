import { useEffect, useMemo, useState } from "react";
import { ArrowDownToLine, ChevronRight, Code2, FileText, LoaderCircle, Search } from "lucide-react";
import { api } from "./api";
import type { Artifact } from "./types";
import { formatSize, phases, reportSpecs } from "./presentation";
import { ReportPreview } from "./ReportPreview";
import { Empty, ErrorNote, Modal, message } from "./ui";

const kindText: Record<string, string> = { image: "图像", csv: "数据表", json: "结构化记录", code: "代码 / 配置", markdown: "Markdown", text: "文本", document: "文档", archive: "压缩包", file: "其他文件" };

export function ArtifactBrowser({ artifacts, onOpen, caseId }: { artifacts: Artifact[]; onOpen: (artifact: Artifact) => void; caseId: string }) {
  const [query, setQuery] = useState(""); const [phase, setPhase] = useState(""); const [kind, setKind] = useState(""); const [page, setPage] = useState(1);
  const filtered = useMemo(() => artifacts.filter(item => (!phase || item.phase === phase) && (!kind || item.kind === kind) && item.path.toLowerCase().includes(query.toLowerCase())).sort((a, b) => a.path.localeCompare(b.path)), [artifacts, phase, kind, query]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / 18)); const currentPage = Math.min(page, pageCount);
  return <section id="files" className="section-block">
    <div className="section-heading"><div><p className="eyebrow">代码 · 图像 · 数据 · 审计</p><h2>全部产物 <span className="count">{artifacts.length}</span></h2></div><ExportButton caseId={caseId} phase={phase || undefined} compact /></div>
    <div className="toolbar files-toolbar">
      <label className="search"><Search size={16} /><input aria-label="搜索产物" placeholder="搜索文件名或路径…" value={query} onChange={event => { setQuery(event.target.value); setPage(1); }} /></label>
      <select aria-label="按阶段筛选产物" value={phase} onChange={event => { setPhase(event.target.value); setPage(1); }}><option value="">所有阶段</option>{Object.entries(phases).map(([id, item]) => <option key={id} value={id}>{item.title}</option>)}</select>
      <select aria-label="按类型筛选产物" value={kind} onChange={event => { setKind(event.target.value); setPage(1); }}><option value="">所有类型</option>{Object.entries(kindText).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select>
    </div>
    {!filtered.length ? <Empty>{artifacts.length ? "没有匹配的产物，试试其他关键词或筛选条件。" : "任务生成的代码、数据、图像与报告将在这里出现。"}</Empty> : <div className="file-list">{filtered.slice((currentPage - 1) * 18, currentPage * 18).map(item => <button className="file-row" key={item.id} onClick={() => onOpen(item)}>
      {item.kind === "image" ? <img src={item.content_url} alt="" loading="lazy" /> : <span className="file-icon">{item.kind === "code" ? <Code2 size={19} /> : <FileText size={19} />}</span>}
      <span className="file-name"><strong>{item.path.split("/").at(-1)}</strong><small>{item.path}</small></span><span className="file-kind">{kindText[item.kind] || "文件"}</span><span className="file-size">{formatSize(item.size_bytes)}</span><ChevronRight size={16} />
    </button>)}</div>}
    <div className="pagination"><span>共 {filtered.length} 份匹配产物{phase && " · 下载按钮按阶段导出"}</span><div><button className="button small" disabled={currentPage <= 1} onClick={() => setPage(currentPage - 1)}>上一页</button><span>{currentPage} / {pageCount}</span><button className="button small" disabled={currentPage >= pageCount} onClick={() => setPage(currentPage + 1)}>下一页</button></div></div>
  </section>;
}

export function ExportButton({ caseId, phase, compact = false }: { caseId: string; phase?: string; compact?: boolean }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  async function start() {
    setBusy(true); setError("");
    try {
      const created = await api.createExport(caseId, phase); let current = await api.getExport(created.export_id);
      for (let attempt = 0; attempt < 180 && !["ready", "failed"].includes(current.status); attempt++) { await new Promise(resolve => setTimeout(resolve, 700)); current = await api.getExport(created.export_id); }
      if (current.download_url) location.assign(current.download_url); else throw new Error(current.error || "导出仍在后台生成，请稍后重试。");
    } catch (reason) { setError(message(reason)); } finally { setBusy(false); }
  }
  return <div className="export-control"><button className={`button ${compact ? "quiet" : "primary"}`} disabled={busy} onClick={() => void start()}>{busy ? <LoaderCircle className="spin" size={16} /> : <ArrowDownToLine size={16} />}{busy ? "正在打包…" : phase ? "下载本阶段" : "下载全部产物"}</button>{error && <span className="inline-error" role="alert">{error}</span>}</div>;
}

export function ArtifactDrawer({ artifact, artifacts, onClose }: { artifact: Artifact; artifacts: Artifact[]; onClose: () => void }) {
  const [preview, setPreview] = useState<{ text?: string; rows?: string[][]; json?: unknown; truncated?: boolean } | null | undefined>(undefined);
  const [error, setError] = useState(""); const [raw, setRaw] = useState(false);
  useEffect(() => { let active = true; void api.getArtifact(artifact.id).then(value => { if (active) setPreview(value.preview); }).catch(reason => { if (active) setError(message(reason)); }); return () => { active = false; }; }, [artifact.id]);
  return <Modal title={reportSpecs.find(spec => artifact.path === `${spec.stem}.md`)?.title || artifact.path.split("/").at(-1)!} onClose={onClose} wide>
    <div className="preview-toolbar"><span>{artifact.path} · {formatSize(artifact.size_bytes)}{preview?.truncated && <strong className="inline-error">当前预览为节选，完整内容请下载原文件。</strong>}</span><div>{artifact.kind === "markdown" && <button className="text-button" onClick={() => setRaw(!raw)}>{raw ? "阅读模式" : "查看 Markdown"}</button>}<a className="button small" href={artifact.download_url}><ArrowDownToLine size={15} /> 下载原文件</a></div></div>
    <div className="preview-body">{artifact.kind === "image" ? <img className="full-image" src={artifact.content_url} alt={artifact.path} /> : error ? <ErrorNote>{error}</ErrorNote> : preview === undefined ? <div className="loading"><LoaderCircle className="spin" /> 正在读取</div> : preview?.text && artifact.kind === "markdown" && !raw ? <ReportPreview text={preview.text} artifact={artifact} artifacts={artifacts} /> : preview?.rows ? <><p className="subtle">表格节选，完整数据请下载原文件。</p><div className="table-scroll"><table><tbody>{preview.rows.map((row, i) => <tr key={i}>{row.map((cell, n) => i === 0 ? <th key={n}>{cell}</th> : <td key={n}>{cell}</td>)}</tr>)}</tbody></table></div></> : preview ? <pre>{preview.text ?? JSON.stringify(preview.json, null, 2)}</pre> : <Empty>此文件暂不支持在线预览，请下载原文件查看。</Empty>}</div>
  </Modal>;
}
