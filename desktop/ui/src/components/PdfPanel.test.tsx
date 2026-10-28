import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api/client";
import type { ReadingPositionResponse, Representation } from "../api/types";
import { PdfPanel } from "./PdfPanel";

const pdf = vi.hoisted(() => ({ getDocument: vi.fn(), getPage: vi.fn(), viewport: vi.fn() }));
vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: {},
  getDocument: pdf.getDocument,
  TextLayer: class { render = vi.fn().mockResolvedValue(undefined); cancel = vi.fn(); },
}));
const representation: Representation = {
  id: "pdf-1", representation_type: "pdf", role: "primary", path_ref: "papers/SkillRL-v2.pdf",
  external_url: "https://arxiv.org/pdf/2601.00001v2", availability: "available", sha256: "hash-v2",
};
const noPosition: ReadingPositionResponse = { position: null, stale: false };
function saved(page: number, row_version = 4): ReadingPositionResponse {
  return { position: { representation_id: "pdf-1", page, row_version, source_hash: "hash-v2" }, stale: false };
}
function setup(props: Partial<Parameters<typeof PdfPanel>[0]> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><PdfPanel representation={representation} assetId="paper-1" {...props} /></QueryClientProvider>);
}
beforeEach(() => {
  pdf.getDocument.mockReset();
  pdf.getPage.mockReset();
  pdf.viewport.mockReset();
  pdf.viewport.mockImplementation(({ scale }) => ({ width: 600 * scale, height: 800 * scale, scale }));
  pdf.getPage.mockResolvedValue({ getTextContent: async () => ({}), getViewport: pdf.viewport,
    render: () => ({ promise: Promise.resolve(), cancel: vi.fn() }) });
  pdf.getDocument.mockImplementation(() => ({ promise: Promise.resolve({ numPages: 12, getPage: pdf.getPage }), destroy: vi.fn() }));
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({} as CanvasRenderingContext2D);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.spyOn(api, "openRepresentation").mockResolvedValue({ kind: "gateway_session", url: "http://localhost:8766/session/pdf-1?token=signed", expires_in: 900 });
  vi.spyOn(api, "readingPosition").mockResolvedValue(noPosition);
  vi.spyOn(api, "saveReadingPosition").mockResolvedValue(saved(2, 5));
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("PDF safe reader", () => {
  it("uses the local open-action despite external metadata, restores without overwriting page 1, and supports zoom", async () => {
    vi.mocked(api.readingPosition).mockResolvedValue(saved(7));
    setup();
    await waitFor(() => expect(screen.getByRole("spinbutton", { name: "跳转到页码" })).toHaveValue(7));
    expect(api.openRepresentation).toHaveBeenCalledWith("pdf-1");
    expect(pdf.getDocument).toHaveBeenCalledWith({ url: "http://localhost:8766/session/pdf-1?token=signed", withCredentials: true });
    expect(screen.getByText(/主版本.*SkillRL-v2.pdf.*来源 v2/)).toBeInTheDocument();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 800)); });
    expect(api.saveReadingPosition).not.toHaveBeenCalled();
    fireEvent.change(screen.getByRole("combobox", { name: "PDF 缩放" }), { target: { value: "1.5" } });
    await waitFor(() => expect(pdf.viewport).toHaveBeenCalledWith({ scale: 1.5 }));
  });

  it("shows Chinese file-fetch failure and retries the same signed session without external fallback", async () => {
    pdf.getDocument.mockImplementationOnce(() => ({ promise: Promise.reject(new TypeError("Failed to fetch")), destroy: vi.fn() }));
    setup();
    expect(await screen.findByRole("alert")).toHaveTextContent("无法取得 PDF 文件");
    fireEvent.click(screen.getByRole("button", { name: "重试加载 PDF" }));
    await screen.findByRole("spinbutton", { name: "跳转到页码" });
    expect(api.openRepresentation).toHaveBeenCalledTimes(1);
    expect(pdf.getDocument).toHaveBeenCalledTimes(2);
    expect(pdf.getDocument.mock.calls.every(([value]) => !value.url.includes("arxiv.org"))).toBe(true);
  });

  it("retries a failed safe-session request rather than using provenance URLs", async () => {
    vi.mocked(api.openRepresentation).mockRejectedValueOnce(new Error("网关未配置"));
    setup();
    expect(await screen.findByRole("alert")).toHaveTextContent("无法创建 PDF 安全会话");
    expect(pdf.getDocument).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "重试安全会话" }));
    await screen.findByRole("spinbutton", { name: "跳转到页码" });
    expect(api.openRepresentation).toHaveBeenCalledTimes(2);
  });
  it("explains a missing registered file without claiming the API is offline", async () => {
    vi.mocked(api.openRepresentation).mockRejectedValueOnce(new ApiError({ status: 404, detail: "Registered material is unavailable." }));
    setup();
    expect(await screen.findByRole("alert")).toHaveTextContent("未找到已登记的 PDF 文件");
    expect(screen.queryByText(/启动或恢复本地 API/)).not.toBeInTheDocument();
    expect(pdf.getDocument).not.toHaveBeenCalled();
  });

  it("refreshes expired sessions, preserving the API's replacement signed URL", async () => {
    pdf.getDocument.mockImplementationOnce(() => ({ promise: Promise.reject(new Error("HTTP 403")), destroy: vi.fn() }));
    setup();
    expect(await screen.findByRole("alert")).toHaveTextContent("安全会话已过期");
    vi.mocked(api.openRepresentation).mockResolvedValueOnce({ kind: "gateway_session", url: "http://localhost:8766/session/pdf-1?token=new-signature" });
    fireEvent.click(screen.getByRole("button", { name: "重试安全会话" }));
    await screen.findByRole("spinbutton", { name: "跳转到页码" });
    expect(pdf.getDocument).toHaveBeenLastCalledWith({ url: "http://localhost:8766/session/pdf-1?token=new-signature", withCredentials: true });
  });

  it("serializes rapid page saves and takes the latest successful ETag", async () => {
    vi.mocked(api.readingPosition).mockResolvedValue(saved(1));
    let finish!: (value: ReadingPositionResponse) => void;
    vi.mocked(api.saveReadingPosition).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }))
      .mockResolvedValueOnce(saved(3, 6));
    setup();
    await screen.findByRole("spinbutton", { name: "跳转到页码" });
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(800); });
    expect(api.saveReadingPosition).toHaveBeenCalledWith("paper-1", { representation_id: "pdf-1", page: 2, source_hash: "hash-v2" }, 4);
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(800); });
    expect(api.saveReadingPosition).toHaveBeenCalledTimes(1);
    await act(async () => { finish(saved(2, 5)); await vi.advanceTimersByTimeAsync(1); });
    expect(api.saveReadingPosition).toHaveBeenLastCalledWith("paper-1", { representation_id: "pdf-1", page: 3, source_hash: "hash-v2" }, 5);
  });

  it("pauses later writes after a conflict and retries only after fetching the current row version", async () => {
    vi.mocked(api.readingPosition).mockResolvedValue(saved(1));
    vi.mocked(api.saveReadingPosition).mockRejectedValueOnce(new Error("HTTP 412：另一窗口已更新阅读位置"));
    setup();
    await screen.findByRole("spinbutton", { name: "跳转到页码" });
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(800); });
    expect(screen.getByRole("status")).toHaveTextContent("后续保存已暂停");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(800); });
    expect(api.saveReadingPosition).toHaveBeenCalledTimes(1);
    vi.mocked(api.readingPosition).mockResolvedValueOnce(saved(9, 8));
    vi.mocked(api.saveReadingPosition).mockResolvedValueOnce(saved(3, 9));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "刷新已保存位置并重试" }));
      await vi.advanceTimersByTimeAsync(800);
    });
    expect(api.saveReadingPosition).toHaveBeenLastCalledWith("paper-1", { representation_id: "pdf-1", page: 3, source_hash: "hash-v2" }, 8);
  });

  it("only accepts whole page numbers within the current document", async () => {
    setup();
    const input = await screen.findByRole("spinbutton", { name: "跳转到页码" });
    fireEvent.change(input, { target: { value: "2.5" } });
    expect(input).toHaveValue(1);
    fireEvent.change(input, { target: { value: "13" } });
    expect(input).toHaveValue(1);
    fireEvent.change(input, { target: { value: "5" } });
    expect(input).toHaveValue(5);
  });
  it("refuses an old page even if a cached response says non-stale when its digest differs", async () => {
    vi.mocked(api.readingPosition).mockResolvedValue({ ...saved(9), position: { ...saved(9).position!, source_hash: "old-digest" } });
    setup();
    expect(await screen.findByRole("spinbutton", { name: "跳转到页码" })).toHaveValue(1);
    expect(screen.getByText(/未自动恢复旧页码/)).toBeInTheDocument();
    expect(api.saveReadingPosition).not.toHaveBeenCalled();
  });

  it("does not draw stale or unverifiable highlights or overwrite a stale reading position on open", async () => {
    vi.mocked(api.readingPosition).mockResolvedValue({ ...saved(8), stale: true, reason: "文件指纹变化" });
    setup({ annotations: [
      { id: "old", asset_id: "paper-1", representation_id: "pdf-1", anchor: { page: 1, representation_id: "pdf-1", source_hash: "old-hash", rects: [{ x: 0, y: 0, width: 1, height: 1 }] } },
      { id: "unknown", asset_id: "paper-1", representation_id: "pdf-1", anchor: { page: 1, representation_id: "pdf-1", rects: [{ x: 0, y: 0, width: 1, height: 1 }] } },
      { id: "current", asset_id: "paper-1", representation_id: "pdf-1", anchor: { page: 1, representation_id: "pdf-1", source_hash: "hash-v2", rects: [{ x: 0.2, y: 0.3, width: 0.1, height: 0.1 }] } },
    ] });
    expect(await screen.findByRole("spinbutton", { name: "跳转到页码" })).toHaveValue(1);
    expect(screen.getByText(/文件指纹变化/)).toBeInTheDocument();
    expect(screen.getByText(/2 条高亮的文件指纹不匹配或无法核验/)).toBeInTheDocument();
    expect(document.querySelectorAll(".pdf-highlights mark")).toHaveLength(1);
    expect(api.saveReadingPosition).not.toHaveBeenCalled();
  });
});
