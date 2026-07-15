import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";


describe("web api client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads the case list from the versioned endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.listCases()).resolves.toEqual({ items: [] });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/cases", undefined);
  });

  it("surfaces the backend detail for failed requests", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "任务不存在" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(api.getCase("missing")).rejects.toThrow("任务不存在");
  });
});
