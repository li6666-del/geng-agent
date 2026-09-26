import { Radio } from "lucide-react";
import type { ReactNode } from "react";

export function Brand() {
  return <a className="brand" href="/" aria-label="BUPT 耿同学通信论文复现首页"><span className="brand-mark"><Radio size={23} /></span><span className="brand-copy"><strong>耿同学<span className="brand-bupt">BUPT</span></strong><small>通信论文复现 · GENG AGENT</small></span></a>;
}
export function message(error: unknown) { return error instanceof Error ? error.message : "操作未完成，请稍后重试。"; }
export function ErrorNote({ children }: { children: ReactNode }) { return <div className="error-note" role="alert">{children}</div>; }
export function formatDate(value: string) {
  const date = new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(date) : "";
}
export const statusText: Record<string, string> = {
  queued: "待处理", running: "处理中", cancel_requested: "正在停止", cancelled: "已停止",
  succeeded: "已完成", failed: "待继续处理", idle: "未开始",
};
export const isActive = (status: string) => ["queued", "running", "cancel_requested"].includes(status);
