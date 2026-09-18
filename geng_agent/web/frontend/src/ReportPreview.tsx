import { Fragment, type ReactNode } from "react";
import type { Artifact } from "./types";
import { reportAsset } from "./presentation";

// Render the Editor's common Markdown structures as React text, never raw HTML.
// Scientific wording is not synthesized or changed by the preview.
export function ReportPreview({ text, artifact, artifacts }: { text: string; artifact: Artifact; artifacts: Artifact[] }) {
  function inline(value: string): ReactNode[] {
    return value.split(/(!?\[[^\]]*\]\([^\n]*?\)|\*\*[^*]+\*\*|`[^`]+`)/g).map((part, i) => {
      const link = part.match(/^(!?)\[([^\]]*)\]\((.*?)\)$/);
      if (link) {
        const target = reportAsset(link[3], artifact.path, artifacts);
        if (link[1]) return target?.kind === "image" ? <figure key={i}><a href={target.content_url} target="_blank" rel="noreferrer"><img src={target.content_url} alt={link[2]} loading="lazy" /></a><figcaption>{link[2]}</figcaption></figure> : <span key={i} className="missing-image">图片不可用：{link[2] || link[3]}</span>;
        if (target) return <a key={i} href={target.download_url}>{link[2]}</a>;
        if (/^https?:\/\//i.test(link[3])) return <a key={i} href={link[3]} target="_blank" rel="noreferrer">{link[2]}</a>;
        return <span key={i}>{link[2]}</span>;
      }
      if (part.startsWith("**") && part.endsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
      if (part.startsWith("`") && part.endsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
      return <Fragment key={i}>{part}</Fragment>;
    });
  }
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  const cells = (line: string) => line.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, "|"));
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) continue;
    if (line.startsWith("```")) {
      const code: string[] = [];
      while (++i < lines.length && !lines[i].startsWith("```")) code.push(lines[i]);
      blocks.push(<pre key={i}><code>{code.join("\n")}</code></pre>);
    } else if (line.includes("|") && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] || "")) {
      const head = cells(line); const rows: string[][] = []; i++;
      while (i + 1 < lines.length && lines[i + 1].includes("|") && lines[i + 1].trim()) rows.push(cells(lines[++i]));
      blocks.push(<div className="table-scroll" key={i}><table><thead><tr>{head.map((cell, n) => <th key={n}>{inline(cell)}</th>)}</tr></thead><tbody>{rows.map((row, r) => <tr key={r}>{row.map((cell, n) => <td key={n}>{inline(cell)}</td>)}</tr>)}</tbody></table></div>);
    } else if (/^#{1,6}\s/.test(line)) {
      const level = line.match(/^#+/)![0].length;
      blocks.push(level <= 2 ? <h2 key={i}>{inline(line.replace(/^#+\s/, ""))}</h2> : <h3 key={i}>{inline(line.replace(/^#+\s/, ""))}</h3>);
    } else if (/^\s*([-*+] |\d+\. )/.test(line)) {
      const ordered = /^\s*\d+\./.test(line); const entries = [line];
      while (i + 1 < lines.length && (ordered ? /^\s*\d+\. / : /^\s*[-*+] /).test(lines[i + 1])) entries.push(lines[++i]);
      const items = entries.map((entry, n) => <li key={n}>{inline(entry.replace(/^\s*(?:[-*+]|\d+\.)\s/, ""))}</li>);
      blocks.push(ordered ? <ol key={i}>{items}</ol> : <ul key={i}>{items}</ul>);
    } else if (/^---+$/.test(line.trim())) blocks.push(<hr key={i} />);
    else if (line.startsWith("> ")) blocks.push(<blockquote key={i}>{inline(line.slice(2))}</blockquote>);
    else blocks.push(<div className="report-paragraph" key={i}>{inline(line)}</div>);
  }
  return <article className="report-prose">{blocks}</article>;
}
