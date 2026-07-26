import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./client";

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("API mutation guards", () => {
  it("sends an exact If-Match and the local CSRF marker", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        id: "annotation-1",
        asset_id: "paper-1",
        body_markdown: "更新",
        row_version: 8,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.updateAnnotation(
      "annotation-1",
      { body_markdown: "更新" },
      7,
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/annotations/annotation-1",
      expect.objectContaining({
        method: "PATCH",
        headers: expect.objectContaining({
          "If-Match": 'W/"7"',
          "X-Research-Hub-CSRF": "1",
        }),
      }),
    );
  });

  it("refuses to PATCH without a concrete row version", async () => {
    vi.stubGlobal("fetch", vi.fn());

    expect(() =>
      api.updateAnnotation("annotation-1", { body_markdown: "更新" }),
    ).toThrow("当前数据没有 row_version");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("does not add the CSRF marker to read-only requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        service: "research-asset-hub",
        status: "healthy",
        checked_at: "2026-07-24T00:00:00Z",
        components: {},
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.health();

    const headers = fetchMock.mock.calls[0]?.[1]?.headers as Record<string, string>;
    expect(headers["X-Research-Hub-CSRF"]).toBeUndefined();
  });
});

describe("search API", () => {
  it("sends paper grouping while allowing an empty browse query", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ items: [], next_cursor: null, total: 0 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.search({
      q: "",
      mode: "hybrid",
      group_by: "paper",
      domain: "robotics",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/search?mode=hybrid&group_by=paper&domain=robotics",
      expect.any(Object),
    );
  });

  it("allows a cold search to run for 120 seconds", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(
      (_path: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener(
            "abort",
            () => reject(init.signal?.reason),
            { once: true },
          );
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const pending = api.search({ q: "agent", group_by: "paper" });
    const settled = vi.fn();
    void pending.then(settled, settled);

    await vi.advanceTimersByTimeAsync(12_000);
    expect(settled).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(108_000);
    await expect(pending).rejects.toMatchObject({
      status: 0,
      title: "请求超时",
    });
  });

  it("keeps the default timeout at 12 seconds for other requests", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(
      (_path: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener(
            "abort",
            () => reject(init.signal?.reason),
            { once: true },
          );
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const pending = api.dashboard();
    const settled = vi.fn();
    void pending.then(settled, settled);

    await vi.advanceTimersByTimeAsync(11_999);
    expect(settled).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(1);
    await expect(pending).rejects.toMatchObject({
      status: 0,
      title: "请求超时",
    });
  });
});
