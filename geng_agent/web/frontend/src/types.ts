export interface User { id: string; email: string }
export interface AuthSession { user: User; csrf_token: string }
export interface SiteConfig { max_pdf_bytes: number; registration_enabled: boolean; artifact_retention_days?: number }
export interface PaperCase {
  id: string;
  display_name: string;
  created_at: string;
  status: string;
  message: string;
  download_url: string | null;
  can_retry: boolean;
  artifacts_expired_at?: string | null;
}

export interface ResultReport {
  id: "comparison" | "reproduction";
  name: string;
  size_bytes: number;
  download_url: string;
}
export interface CaseResult {
  case_id: string;
  available: boolean;
  message: string;
  finished_at: string | null;
  bundle: { download_url: string; size_bytes: number } | null;
  reports: ResultReport[];
  excerpt: string[];
  tasks: { directory: string; name: string; code_files: number; result_files: number; readme: string | null }[];
}
