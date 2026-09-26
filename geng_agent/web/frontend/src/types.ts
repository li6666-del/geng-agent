export interface User { id: string; email: string }
export interface AuthSession { user: User; csrf_token: string }
export interface SiteConfig { max_pdf_bytes: number; registration_enabled: boolean }
export interface PaperCase {
  id: string;
  display_name: string;
  created_at: string;
  status: string;
  message: string;
  download_url: string | null;
  can_retry: boolean;
}
