import { afterEach, describe, expect, it, vi } from "vitest";
import { api, resolveApiBase } from "./client";

type HubWindow = Window & { __HUB_API_BASE__?: string };

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

    await api.updateAnnotation("annotation-1", { body_markdown: "更新" }, 7);

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

    const headers = fetchMock.mock.calls[0]?.[1]?.headers as Record<
      string,
      string
    >;
    expect(headers["X-Research-Hub-CSRF"]).toBeUndefined();
  });
});

describe("list cursors", () => {
  it("passes cursors and filters without losing paginated response metadata", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => jsonResponse({ items: [], next_cursor: "next", total: 61 }));
    vi.stubGlobal("fetch", fetchMock);
    const cursor = "offset/30+=";
    expect(await api.jobs(undefined, cursor)).toEqual({ items: [], next_cursor: "next", total: 61 });
    await api.annotations("asset-1", undefined, cursor);
    await api.conflicts(undefined, cursor, "pending");
    const paths = fetchMock.mock.calls.map(([path]) => new URL(path as string, "http://localhost"));
    expect(paths.map((url) => url.searchParams.get("cursor"))).toEqual([cursor, cursor, cursor]);
    expect(paths[1]?.searchParams.get("asset_id")).toBe("asset-1");
    expect(paths[2]?.searchParams.get("status")).toBe("pending");
  });
});

describe("conflict resolution", () => {
  it.each([{ value: "合并题名" }, { value: ["作者一", "作者二"] }, { value: 2025 }, { value: null }])("sends the merge value with the current row version", async ({ value }) => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: "c1", status: "resolved" }));
    vi.stubGlobal("fetch", fetchMock);
    await api.resolveConflict("c1", "merge", 6, value);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/sync-conflicts/c1/resolve", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ strategy: "merge", resolved_value: value }),
      headers: expect.objectContaining({ "If-Match": 'W/"6"' }),
    }));
  });
});

describe("health API", () => {
  it("accepts a slow but successful database health check", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_path: RequestInfo | URL, init?: RequestInit) =>
          new Promise<Response>((resolve, reject) => {
            setTimeout(
              () =>
                resolve(
                  jsonResponse({
                    service: "research-asset-hub",
                    status: "healthy",
                    components: {},
                  }),
                ),
              9_000,
            );
            init?.signal?.addEventListener(
              "abort",
              () => reject(init.signal?.reason),
              { once: true },
            );
          }),
      ),
    );
    const result = api.health().then(
      (response) => ({ status: response.status }),
      () => ({ status: "failed" }),
    );
    await vi.advanceTimersByTimeAsync(9_000);
    await expect(result).resolves.toEqual({ status: "healthy" });
  });
});

describe("search API", () => {
  it("sends paper grouping while allowing an empty browse query", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ items: [], next_cursor: null, total: 0 }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await api.search({
      q: "",
      mode: "keyword",
      group_by: "paper",
      domain: "robotics",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/search?mode=keyword&sort=date_desc&group_by=paper&domain=robotics",
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

describe("resolveApiBase", () => {
  it("prefers the shell-injected value on Tauri", () => {
    vi.stubGlobal("window", {
      __HUB_API_BASE__: "http://127.0.0.1:8767/api/v1",
    } as HubWindow);
    expect(resolveApiBase()).toBe("http://127.0.0.1:8767/api/v1");
    vi.unstubAllGlobals();
  });

  it("falls back to the build-time env value", () => {
    vi.stubEnv("VITE_API_BASE", "http://127.0.0.1:9000/api/v1");
    expect(resolveApiBase()).toBe("http://127.0.0.1:9000/api/v1");
    vi.unstubAllEnvs();
  });

  it("defaults to the dev-proxied relative path", () => {
    expect(resolveApiBase()).toBe("/api/v1");
  });
});

describe("product workflow API contracts", () => {
  it("sends raw PDF bytes with the filename and CSRF marker", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ id: "d1", status: "ready" }));
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["%PDF-1.7"], "论文.pdf", {
      type: "application/pdf",
    });
    await api.uploadIntakePdf("d1", file);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/intake/drafts/d1/pdf?filename=%E8%AE%BA%E6%96%87.pdf",
      expect.objectContaining({
        method: "PUT",
        body: file,
        headers: expect.objectContaining({
          "Content-Type": "application/pdf",
          "X-Research-Hub-CSRF": "1",
        }),
      }),
    );
  });
  it("protects reading-position updates with their current version", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({
          position: { representation_id: "r1", page: 9, row_version: 4 },
          stale: false,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    await api.saveReadingPosition(
      "a1",
      { representation_id: "r1", page: 9 },
      3,
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/assets/a1/reading-position",
      expect.objectContaining({
        method: "PUT",
        headers: expect.objectContaining({ "If-Match": 'W/"3"' }),
      }),
    );
  });
  it("uploads a package as bytes and commits its reviewed identity mapping using If-Match", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(
          jsonResponse({ id: "i1", status: "preview", row_version: 2 }),
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["zip"], "domain.zip", { type: "application/zip" });
    await api.importPackage(file);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/imports/packages?filename=domain.zip",
      expect.objectContaining({ body: file }),
    );
    await api.commitPackage(
      "i1",
      { identity_mapping: { old: null }, include_user_state: false },
      2,
    );
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/imports/packages/i1/commit",
      expect.objectContaining({
        headers: expect.objectContaining({ "If-Match": 'W/"2"' }),
        body: JSON.stringify({
          identity_mapping: { old: null },
          include_user_state: false,
        }),
      }),
    );
  });
});

describe("workflow collection contracts", () => {
  it("loads the full collection directory across cursors", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          items: [{ id: "c1", label: "One" }],
          next_cursor: "next",
          total: 2,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          items: [{ id: "c2", label: "Two" }],
          next_cursor: null,
          total: 2,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const result = await api.collections();
    expect(result.items.map((item) => item.id)).toEqual(["c1", "c2"]);
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/collections?cursor=next",
      expect.any(Object),
    );
  });
  it("keeps version checks for deleting collections and saved searches", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(new Response(null, { status: 204 })),
      );
    vi.stubGlobal("fetch", fetchMock);
    await api.deleteCollection("c1", 4);
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/collections/c1",
      expect.objectContaining({
        headers: expect.objectContaining({ "If-Match": 'W/"4"' }),
      }),
    );
    await api.deleteSavedSearch("s1", 7);
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/saved-searches/s1",
      expect.objectContaining({
        headers: expect.objectContaining({ "If-Match": 'W/"7"' }),
      }),
    );
  });
});
