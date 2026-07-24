import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./client";

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
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
