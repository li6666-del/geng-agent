import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { formatDate, mergeEvents, outcomeLabel, reportAsset } from "./presentation";
import { ReportPreview } from "./ReportPreview";
import type { Artifact, EventPayload } from "./types";

const artifact = (path: string, kind = "image") => ({ id: path, path, kind, content_url: `/api/v1/artifacts/${encodeURIComponent(path)}/content`, download_url: "/download" }) as Artifact;
const local = artifact("report_assets/T1/local.png");
const paper = artifact("report_assets/T1/paper.png");
const report = artifact("result_review.md", "markdown");

describe("research presentation", () => {
  it("treats SQLite timestamps as UTC before formatting in the browser timezone", () => {
    expect(formatDate("2026-09-13T05:26:55")).toBe(formatDate("2026-09-13T05:26:55Z"));
    expect(formatDate("invalid")).toBe("—");
  });
  it("keeps all scientific terminal outcomes distinct from unknown", () => {
    expect(outcomeLabel("inconclusive_missing_information")).toBe("信息不足");
    expect(outcomeLabel("not_reproduced")).toBe("未复现");
    expect(outcomeLabel(null)).toBe("待独立核验");
    expect(outcomeLabel("unexpected")).toContain("未识别");
  });
  it("deduplicates persisted and reconnecting events in order", () => {
    const event = (id: number) => ({ id }) as EventPayload;
    expect(mergeEvents([event(2), event(1)], [event(2), event(3)]).map(item => item.id)).toEqual([1, 2, 3]);
  });
  it("resolves only case-local catalogued report images", () => {
    expect(reportAsset(local.path, report.path, [local])).toBe(local);
    expect(reportAsset("../" + local.path, "reports/detail.md", [local])).toBe(local);
    for (const path of ["../secret.png", "https://external/pixel.png", "//external/pixel.png", "%2e%2e/secret.png", "file:///secret.png", "javascript:alert(1)"]) {
      expect(reportAsset(path, report.path, [local])).toBeUndefined();
    }
  });
  it("renders paired images and original numbers without executing HTML", () => {
    const html = renderToStaticMarkup(<ReportPreview artifact={report} artifacts={[local, paper]} text={`# 对比\n\n| 本地结果 | 原文结果 |\n|---|---|\n| ![本地图](${local.path}) | ![原图](${paper.path}) |\n\n偏差 −3.25 dB。\n<script>alert(1)</script>\n![外部图](https://example.com/pixel.png)`} />);
    expect((html.match(/<img /g) || []).length).toBe(2);
    expect(html).toContain("−3.25 dB");
    expect(html).toContain("<table>");
    expect(html).not.toContain("<script>");
    expect(html).toContain("图片不可用");
    expect(html).not.toContain('src="https:');
  });
});
