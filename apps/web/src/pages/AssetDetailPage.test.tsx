import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { Annotation, ApiList, Asset, Representation } from "../api/types";
import { CompareProvider } from "../state/CompareContext";
import { AssetDetailPage } from "./AssetDetailPage";

afterEach(() => vi.restoreAllMocks());
vi.mock("../components/PdfPanel", () => ({ PdfPanel: ({ representation, initialPage }: { representation?: Representation; initialPage?: number }) => <div data-testid="pdf-reader">{representation?.id}:{initialPage ?? "saved"}</div> }));
function setup(asset: Asset, annotations: ApiList<Annotation> = { items: [], next_cursor: null }, representations: Representation[] = [], suffix = "") {
  const read = vi.spyOn(api, "asset").mockResolvedValue(asset);
  vi.spyOn(api, "representations").mockResolvedValue(representations);
  vi.spyOn(api, "readingPosition").mockResolvedValue({ position: { representation_id: "pdf-saved", page: 7, source_hash: "saved-hash" }, stale: false });
  vi.spyOn(api, "annotations").mockResolvedValue(annotations);
  vi.spyOn(api, "collections").mockResolvedValue({ items: [], next_cursor: null });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <CompareProvider>
        <MemoryRouter initialEntries={[`/assets/${asset.id}${suffix}`]}>
          <Routes><Route path="/assets/:assetId" element={<AssetDetailPage />} /></Routes>
        </MemoryRouter>
      </CompareProvider>
    </QueryClientProvider>,
  );
  return read;
}
describe("paper metadata entry", () => {
  const versions: Representation[] = [
    { id: "pdf-first", representation_type: "pdf", role: "original", path_ref: "papers/paper-v1.pdf", availability: "available" },
    { id: "pdf-saved", representation_type: "pdf", role: "primary", path_ref: "papers/paper-v2.pdf", availability: "available" },
  ];
  it("resumes the saved PDF representation rather than the first version and keeps version labels distinct", async () => {
    setup({ id: "paper", title: "Paper", asset_type: "paper" }, undefined, versions, "?view=pdf");
    expect(await screen.findByTestId("pdf-reader")).toHaveTextContent("pdf-saved:saved");
    expect(screen.getByRole("combobox", { name: "选择 PDF 版本" })).toHaveValue("pdf-saved");
    expect(screen.getByRole("option", { name: /原始版本.*paper-v1.pdf/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /主版本.*paper-v2.pdf/ })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "选择 PDF 版本" }), { target: { value: "pdf-first" } });
    expect(await screen.findByTestId("pdf-reader")).toHaveTextContent("pdf-first:saved");
  });
  it("honors an explicit PDF anchor over the saved version", async () => {
    setup({ id: "paper", title: "Paper", asset_type: "paper" }, undefined, versions, "?view=pdf&representation=pdf-first&page=3");
    expect(await screen.findByTestId("pdf-reader")).toHaveTextContent("pdf-first:3");
  });
  it("does not silently replace an explicitly requested missing PDF with another version", async () => {
    setup({ id: "paper", title: "Paper", asset_type: "paper" }, undefined, versions, "?view=pdf&representation=pdf-absent&page=3");
    expect(await screen.findByText(/这条批注所对应的 PDF 当前不可用/)).toBeInTheDocument();
    expect(await screen.findByTestId("pdf-reader")).toHaveTextContent(":3");
    expect(screen.getByTestId("pdf-reader")).not.toHaveTextContent("pdf-first");
  });
  it("opens the editor from paper detail and shows the saved title after closing", async () => {
    const asset: Asset = { id: "paper-1", title: "Old title", asset_type: "paper", row_version: 7 };
    const result = { ...asset, title: "Correct title", row_version: 8 };
    const update = vi.spyOn(api, "updateAsset").mockResolvedValue(result);
    const read = setup(asset);
    fireEvent.click(await screen.findByRole("button", { name: "编辑元数据" }));
    const dialog = screen.getByRole("dialog", { name: "编辑论文元数据" });
    expect(within(dialog).getByText(/来源清单、PDF 和其他原文件保持只读/)).toBeInTheDocument();
    fireEvent.change(within(dialog).getByLabelText("题名 *"), { target: { value: "Correct title" } });
    read.mockResolvedValue(result);
    fireEvent.click(within(dialog).getByRole("button", { name: "保存元数据" }));
    await screen.findByRole("heading", { level: 1, name: "Correct title" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(update).toHaveBeenCalledWith(asset.id, { title: "Correct title" }, 7);
  });
  it("does not offer the paper editor for a repository", async () => {
    setup({ id: "repository-1", title: "Repository", asset_type: "repository", row_version: 2 });
    await screen.findByRole("heading", { level: 1, name: "Repository" });
    expect(screen.queryByRole("button", { name: "编辑元数据" })).not.toBeInTheDocument();
  });
  it("closes the metadata dialog with Escape and restores focus to its trigger", async () => {
    setup({ id: "paper-1", title: "Paper", asset_type: "paper", row_version: 1 });
    const trigger = await screen.findByRole("button", { name: "编辑元数据" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "编辑论文元数据" });
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });
  it("keeps the first annotation page after a load failure and retries the cursor without losing PDF anchors", async () => {
    setup({ id: "paper-1", title: "Paper", asset_type: "paper", row_version: 1 }, {
      items: [{ id: "note-1", asset_id: "paper-1", body_markdown: "First note", anchor: { page: 3, representation_id: "pdf-1", rects: [] } }],
      next_cursor: "page-2",
    });
    await screen.findByText("First note");
    vi.mocked(api.annotations).mockRejectedValueOnce(new Error("暂时离线"))
      .mockResolvedValueOnce({ items: [{ id: "note-2", asset_id: "paper-1", body_markdown: "Second note" }], next_cursor: null });
    fireEvent.click(screen.getByRole("button", { name: "加载更多批注" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("后续批注加载失败");
    expect(screen.getByText("First note")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "回到 PDF 第 3 页" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试加载更多批注" }));
    await screen.findByText("Second note");
    expect(screen.getByText("First note")).toBeInTheDocument();
    expect(screen.getByText("已加载 2")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "加载更多批注" })).not.toBeInTheDocument();
    expect(api.annotations).toHaveBeenLastCalledWith("paper-1", expect.any(AbortSignal), "page-2");
  });
});
