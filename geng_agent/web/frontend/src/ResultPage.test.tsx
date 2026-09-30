import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { CaseResult, PaperCase } from "./types";
import { ResultContents, resultHref, resultId } from "./ResultPage";

const paper: PaperCase = { id: "c1", display_name: "无线信道研究", created_at: "2026-09-30T00:00:00Z", status: "succeeded", message: "已完成", download_url: "/api/v1/cases/c1/download", can_retry: false };
const result: CaseResult = {
  case_id: "c1", available: true, message: "", finished_at: "2026-09-30T01:00:00Z",
  bundle: { download_url: "/api/v1/cases/c1/download", size_bytes: 1048576 },
  reports: [
    { id: "comparison", name: "论文复现结果对比报告.docx", size_bytes: 12000, download_url: "/api/v1/cases/c1/reports/comparison" },
    { id: "reproduction", name: "本地复现报告.docx", size_bytes: 18000, download_url: "/api/v1/cases/c1/reports/reproduction" },
  ],
  excerpt: ["部分结论在当前假设下未复现。"],
  tasks: [{ directory: "t01_T1", name: "信道模型与误码率", code_files: 3, result_files: 2, readme: "# 信道模型\nchannel.py：生成衰落信道。\n<script>alert(1)</script>" }],
};

describe("result detail page", () => {
  it("shows actual deliverables and preserves the report's uncertainty", () => {
    const html = renderToStaticMarkup(<ResultContents paper={paper} result={result} />);
    expect(html).toContain("无线信道研究");
    expect(html).toContain("部分结论在当前假设下未复现。");
    expect(html).toContain("对比报告原文节选");
    expect(html).toContain("信道模型与误码率");
    expect(html).toContain("channel.py：生成衰落信道。");
    expect(html).toContain('href="/api/v1/cases/c1/reports/comparison"');
    expect(html).toContain('href="/api/v1/cases/c1/reports/reproduction"');
    expect(html).toContain('href="/api/v1/cases/c1/download"');
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
  });

  it("does not invent conclusions or report downloads for an unavailable result", () => {
    const html = renderToStaticMarkup(<ResultContents paper={paper} result={{ ...result, available: false, message: "交付包暂不可用", bundle: null, reports: [], tasks: [], excerpt: [] }} />);
    expect(html).toContain("交付包暂不可用");
    expect(html).not.toContain("交付包已就绪");
    expect(html).not.toContain("下载完整交付包");
    expect(html).not.toContain("报告导读");
  });

  it("retains downloads when a historical report has no readable excerpt", () => {
    const html = renderToStaticMarkup(<ResultContents paper={paper} result={{ ...result, excerpt: [] }} />);
    expect(html).toContain("下载完整交付包");
    expect(html).toContain("论文复现结果对比报告");
    expect(html).not.toContain("报告导读");
  });

  it("supports persistent result addresses without throwing on malformed links", () => {
    expect(resultId(resultHref("case 1"))).toBe("case 1");
    expect(resultId("#/results/%ZZ")).toBeNull();
    expect(resultId("#")).toBeNull();
    expect(resultId("#/results/a/other")).toBeNull();
  });

  it("offers a new run instead of downloads after retention cleanup", () => {
    const html = renderToStaticMarkup(<ResultContents paper={{ ...paper, artifacts_expired_at: "2026-09-30T00:00:00Z", can_retry: true }} result={{ ...result, available: false, bundle: null, reports: [], tasks: [], excerpt: [], message: "交付文件已按保留策略清理。" }} />);
    expect(html).toContain("原论文仍保留");
    expect(html).toContain("重新复现");
    expect(html).not.toContain("下载完整交付包");
  });
});
