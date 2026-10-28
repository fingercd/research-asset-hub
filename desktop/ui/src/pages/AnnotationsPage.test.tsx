import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api/client";
import { AnnotationsPage } from "./AnnotationsPage";

function renderAnnotations() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter><AnnotationsPage /></MemoryRouter></QueryClientProvider>);
}

afterEach(() => { vi.restoreAllMocks(); });

describe("AnnotationsPage", () => {
  it("makes the loaded filter scope explicit and finds matching annotations on the second page", async () => {
    const annotations = vi.spyOn(api, "annotations").mockImplementation(async (_assetId, _signal, cursor) => ({
      items: [{ id: cursor ? "a2" : "a1", asset_id: "asset-1", body_markdown: cursor ? "后页匹配内容" : "首批内容", row_version: 1 }],
      next_cursor: cursor ? null : "page-2",
    }));
    renderAnnotations();
    await screen.findByText("首批内容");
    fireEvent.change(screen.getByRole("searchbox", { name: "筛选已加载批注" }), { target: { value: "匹配" } });
    expect(screen.getByText("已加载批注中没有匹配内容")).toBeInTheDocument();
    expect(screen.getByText(/筛选仅作用于已加载内容/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "加载更多批注" }));
    await screen.findByText("后页匹配内容");
    expect(annotations).toHaveBeenLastCalledWith(undefined, expect.any(AbortSignal), "page-2");
    expect(screen.queryByRole("button", { name: "加载更多批注" })).not.toBeInTheDocument();
  });

  it("shows a delete failure and retries deletion without losing the annotation", async () => {
    const annotation = { id: "a1", asset_id: "asset-1", body_markdown: "保留的批注", row_version: 4 };
    let deleted = false;
    vi.spyOn(api, "annotations").mockImplementation(async () => ({ items: deleted ? [] : [annotation], next_cursor: null }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const remove = vi.spyOn(api, "deleteAnnotation").mockRejectedValueOnce(new Error("无法连接本地 API"))
      .mockImplementationOnce(async () => { deleted = true; });
    renderAnnotations();
    await screen.findByText("保留的批注");
    fireEvent.click(screen.getByRole("button", { name: "删除批注" }));
    await screen.findByText(/删除失败：无法连接本地 API/);
    expect(screen.getByText("保留的批注")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试删除" }));
    await screen.findByText("还没有批注");
    expect(remove).toHaveBeenNthCalledWith(2, "a1", 4);
  });

  it("refreshes changed versions before the user confirms deletion again", async () => {
    let currentVersion = 1;
    let deleted = false;
    vi.spyOn(api, "annotations").mockImplementation(async () => ({ items: deleted ? [] : [{ id: "a1", asset_id: "asset-1", body_markdown: currentVersion === 1 ? "原批注" : "最新批注", row_version: currentVersion }], next_cursor: null }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const remove = vi.spyOn(api, "deleteAnnotation")
      .mockRejectedValueOnce(new ApiError({ status: 412, detail: "版本冲突" }))
      .mockImplementationOnce(async () => { deleted = true; });
    renderAnnotations();
    await screen.findByText("原批注");
    fireEvent.click(screen.getByRole("button", { name: "删除批注" }));
    await screen.findByText(/删除失败：版本冲突/);
    currentVersion = 2;
    fireEvent.click(screen.getByRole("button", { name: "刷新最新批注" }));
    await screen.findByText("最新批注");
    await waitFor(() => expect(screen.queryByText(/删除失败/)).not.toBeInTheDocument());
    expect(remove).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "删除批注" }));
    await screen.findByText("还没有批注");
    expect(remove).toHaveBeenLastCalledWith("a1", 2);
  });

  it("keeps deletion recovery available when refreshing the latest version fails", async () => {
    vi.spyOn(api, "annotations").mockResolvedValueOnce({ items: [{ id: "a1", asset_id: "asset-1", body_markdown: "现有批注", row_version: 1 }], next_cursor: null })
      .mockRejectedValueOnce(new Error("刷新服务不可用"));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(api, "deleteAnnotation").mockRejectedValueOnce(new ApiError({ status: 412, detail: "版本冲突" }));
    renderAnnotations();
    await screen.findByText("现有批注");
    fireEvent.click(screen.getByRole("button", { name: "删除批注" }));
    await screen.findByText(/删除失败：版本冲突/);
    fireEvent.click(screen.getByRole("button", { name: "刷新最新批注" }));
    await screen.findByText("刷新服务不可用");
    expect(screen.getByText("现有批注")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新最新批注" })).toBeInTheDocument();
  });
});
