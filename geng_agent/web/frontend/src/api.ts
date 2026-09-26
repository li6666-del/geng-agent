import type { AuthSession, PaperCase, SiteConfig } from "./types";

let csrfToken = "";

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.method && init.method !== "GET" && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(url, { ...init, headers, credentials: "same-origin" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    if (response.status === 401 && !url.startsWith("/api/v1/auth/") && typeof window !== "undefined") {
      window.dispatchEvent(new Event("session-expired"));
    }
    throw new ApiError(typeof body.detail === "string" ? body.detail : "提交信息不完整，请检查后重试。", response.status);
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}

async function authenticate(action: "login" | "register", email: string, password: string) {
  const result = await request<AuthSession>(`/api/v1/auth/${action}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }),
  });
  csrfToken = result.csrf_token;
  return result.user;
}

export const api = {
  site: () => request<SiteConfig>("/api/v1/site"),
  session: async () => {
    try {
      const result = await request<AuthSession>("/api/v1/auth/session");
      csrfToken = result.csrf_token;
      return result.user;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) { csrfToken = ""; return null; }
      throw error;
    }
  },
  authenticate,
  logout: async () => {
    try { await request<void>("/api/v1/auth/logout", { method: "POST" }); }
    catch (error) { if (!(error instanceof ApiError && error.status === 401)) throw error; }
    csrfToken = "";
  },
  listCases: () => request<{ items: PaperCase[] }>("/api/v1/cases"),
  createCase: (body: FormData) => request<{ case_id: string }>("/api/v1/cases", { method: "POST", body }),
  retryCase: (id: string) => request(`/api/v1/cases/${id}/retry`, { method: "POST" }),
  cancelCase: (id: string) => request(`/api/v1/cases/${id}/cancel`, { method: "POST" }),
};
