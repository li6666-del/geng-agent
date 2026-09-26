import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AuthPage, CaseRow, DeliveryContents } from "./App";

describe("simple paper delivery interface", () => {
  it("offers email login and the three final deliverables", () => {
    const html = renderToStaticMarkup(<AuthPage site={{ max_pdf_bytes: 83886080, registration_enabled: true }} onLogin={() => {}} />);
    expect(html).toContain('type="email"');
    expect(html).toContain('type="password"');
    expect(html).toContain("创建账号");
    const contents = renderToStaticMarkup(<DeliveryContents />);
    expect(contents).toContain("论文复现结果对比报告");
    expect(contents).toContain("本地复现报告");
    expect(contents).toContain("分任务复现项目");
  });

  it("offers one final bundle without a stage timeline or intermediate files", () => {
    const html = renderToStaticMarkup(<CaseRow item={{ id: "c1", display_name: "示例论文", created_at: "2026-09-26T12:00:00", status: "succeeded", message: "已完成交付", download_url: "/api/v1/cases/c1/download", can_retry: false }} busy={false} onAction={() => {}} />);
    expect(html).toContain("下载交付包");
    expect(html).not.toMatch(/阶段|中间产物|独立 Reporter|实时更新/);
    expect(html.match(/<a /g)).toHaveLength(1);
  });
});
