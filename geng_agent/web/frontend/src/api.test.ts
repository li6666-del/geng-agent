import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

describe("account and final-delivery API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("restores the session and includes CSRF on uploads", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ user: { id: "u1", email: "a@b.com" }, csrf_token: "csrf-test" })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ case_id: "case1" })));
    vi.stubGlobal("fetch", fetch);
    await api.session();
    await api.createCase(new FormData());
    expect(fetch.mock.calls[1][1].headers.get("X-CSRF-Token")).toBe("csrf-test");
    expect(fetch.mock.calls[1][1].credentials).toBe("same-origin");
  });

  it("treats an expired session as signed out", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "请先登录" }), { status: 401 })));
    expect(await api.session()).toBeNull();
  });

  it("handles empty logout responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
    await expect(api.logout()).resolves.toBeUndefined();
  });

  it("allows signing out after the server session expires", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "请先登录" }), { status: 401 })));
    await expect(api.logout()).resolves.toBeUndefined();
  });

  it("surfaces server errors as readable text", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "论文任务不存在" }), { status: 404 })));
    await expect(api.retryCase("missing")).rejects.toThrow("论文任务不存在");
  });
});
