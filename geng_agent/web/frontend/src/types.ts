export type PhaseState = "waiting" | "running" | "partial" | "success" | "failed" | "cancelled";

export interface Job {
  id: string;
  case_id: string;
  status: string;
  current_phase: string | null;
  current_step: string | null;
  cancel_requested: boolean;
  attempt: number;
  error: { code: string; message: string } | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Phase {
  id: string;
  index: number;
  label: string;
  steps: string[];
  state: PhaseState;
  artifact_count: number;
}

export interface Artifact {
  id: string;
  phase: string;
  path: string;
  kind: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  content_url: string;
  download_url: string;
}

export interface CaseSummary {
  id: string;
  display_name: string;
  source: string;
  created_at: string;
  job: Job | null;
}

export interface CaseDetail extends CaseSummary {
  phases: Phase[];
  artifacts: Artifact[];
}

export interface EventPayload {
  id: number;
  type: string;
  phase?: string;
  step?: string;
  message?: string;
  data?: Record<string, unknown>;
  created_at: string;
}
