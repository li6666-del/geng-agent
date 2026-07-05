import type { Artifact, CaseDetail, CaseSummary, EventPayload } from "./types";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  listCases: () => request<{ items: CaseSummary[] }>("/api/v1/cases"),
  getCase: (id: string) => request<CaseDetail>(`/api/v1/cases/${id}`),
  createCase: (form: FormData) => request<{ case_id: string; job_id: string }>("/api/v1/cases", { method: "POST", body: form }),
  cancelJob: (id: string) => request<{ status: string }>(`/api/v1/jobs/${id}/cancel`, { method: "POST" }),
  getArtifact: (id: string) => request<Artifact & { preview: { text?: string; rows?: string[][]; json?: unknown } | null }>(`/api/v1/artifacts/${id}`),
  createExport: (caseId: string, phase?: string) => request<{ export_id: string; status: string }>(
    `/api/v1/cases/${caseId}/exports${phase ? `?phase=${encodeURIComponent(phase)}` : ""}`,
    { method: "POST" },
  ),
  getExport: (id: string) => request<{ id: string; status: string; download_url: string | null; error: string | null }>(`/api/v1/exports/${id}`),
};

export function connectEvents(jobId: string, onEvent: (event: EventPayload) => void, onState: (connected: boolean) => void): EventSource {
  const stream = new EventSource(`/api/v1/jobs/${jobId}/events/live`);
  const types = ["job.started", "job.retrying", "phase.started", "step.completed", "phase.completed", "job.finished", "job.failed", "job.cancelled"];
  types.forEach((type) => stream.addEventListener(type, (raw) => onEvent(JSON.parse((raw as MessageEvent).data) as EventPayload)));
  stream.onopen = () => onState(true);
  stream.onerror = () => onState(false);
  return stream;
}
