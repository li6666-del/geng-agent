import type { Artifact, EventPayload } from "./types";

export const phases: Record<string, { title: string; actor: string; description: string }> = {
  paper_analysis: { title: "论文理解", actor: "理解智能体", description: "解析正文与图表，联合提取核心事实、论文主张和信息缺口。" },
  repro_design: { title: "实验规划", actor: "规划智能体", description: "按需回补关键缺口，联合确定实验任务、验收依据和科学架构。" },
  task_reproduction: { title: "复现与独立核验", actor: "Writer · Reporter", description: "按需构建共享 Foundation；Writer 执行实验，Reporter 独立核验并决定是否修订。" },
  report_composition: { title: "中文报告编辑", actor: "Report Editor", description: "依据核验记录撰写结果对比与本地复现报告，保留假设、差距和人工核查建议。" },
  report_delivery: { title: "项目与报告交付", actor: "文件交付", description: "转换 Word，整理代码、配置、依赖说明和执行证据。" },
};

export const jobText: Record<string, string> = {
  queued: "排队中", running: "处理中", cancel_requested: "正在停止", succeeded: "流程已结束", failed: "流程中断", cancelled: "已停止",
};
export const outcomeText: Record<string, string> = {
  reproduced: "已复现", reproduced_with_assumptions: "带假设复现", not_reproduced: "未复现", inconclusive_missing_information: "信息不足",
  execution_failed: "执行失败", review_incomplete: "核验未完成",
};
export const engineeringText: Record<string, string> = {
  verified: "执行证据已核验", execution_failed: "执行失败", handoff_failed: "交接失败", evidence_invalid: "执行证据无效", unverified_execution: "执行尚未核验",
};
export const stateText: Record<string, string> = { waiting: "等待处理", running: "处理中", partial: "部分完成", success: "已完成", failed: "已中断", cancelled: "已停止" };
export const stepText: Record<string, string> = {
  start: "初始化案例", mineru_layout: "解析论文版面", facts_initial: "论文理解：事实与主张", thesis: "发布论文主张",
  tasks_preliminary: "联合规划实验", facts: "按需回补事实", tasks: "发布复现任务", experiment_index: "整理实验索引",
  scientific_architecture: "发布科学架构", environment: "准备执行环境", environment_lock: "准备执行环境",
  foundation: "构建共享 Foundation", generation: "任务执行与核验迭代", runtime: "汇总执行证据", task_reporters: "汇总独立核验",
  report_editor: "编辑中文报告", reports: "整理项目与报告",
};
export const reportSpecs = [
  { stem: "result_review", title: "论文复现结果对比报告", label: "先看结论", description: "逐任务查看核心事实与假设、本地图与原文图、结果差距及人工核查建议。" },
  { stem: "reproduction_report", title: "本地复现报告", label: "深入核查", description: "核查实现、参数来源、配置、依赖、运行记录和交付限制。" },
];
export function outcomeLabel(value: string | null) { return value ? outcomeText[value] || `未识别结论（${value}）` : "待独立核验"; }
export function isActive(status?: string) { return ["queued", "running", "cancel_requested"].includes(status || ""); }
export function mergeEvents(...groups: EventPayload[][]) {
  return [...new Map(groups.flat().map(event => [event.id, event])).values()].sort((a, b) => a.id - b.id).slice(-80);
}
export function formatDate(value?: string | null) {
  if (!value) return "—";
  // SQLite drops the offset from the backend's UTC timestamps.
  const date = new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`);
  if (!Number.isFinite(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
}
export function formatSize(bytes: number) { return bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`; }

// Resolve report links only against artifacts supplied by this case's API.
export function reportAsset(target: string, current: string, artifacts: Artifact[]): Artifact | undefined {
  let decoded: string;
  try { decoded = decodeURIComponent(target.trim().replace(/^<|>$/g, "")); } catch { return; }
  if (/^[a-z][a-z\d+.-]*:|^[\\/]|[\u0000-\u001f]/i.test(decoded)) return;
  const parts = current.split("/").slice(0, -1);
  for (const part of decoded.replace(/\\/g, "/").split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") { if (!parts.length) return; parts.pop(); } else parts.push(part);
  }
  return artifacts.find(artifact => artifact.path === parts.join("/"));
}
