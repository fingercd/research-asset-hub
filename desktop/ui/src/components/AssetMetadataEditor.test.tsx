import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api/client";
import type { Asset } from "../api/types";
import { AssetMetadataEditor } from "./AssetMetadataEditor";

const asset: Asset = {
  id: "paper-1", asset_type: "paper", title: "Original title", row_version: 7,
  authors: ["Alice"], year: 2025, venue: "ICLR", summary: "Old summary",
};
function setup(input = asset) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  client.setQueryData(["asset", input.id], input);
  const close = vi.fn();
  render(<QueryClientProvider client={client}><AssetMetadataEditor asset={input} onClose={close} /></QueryClientProvider>);
  return { client, close };
}
afterEach(() => vi.restoreAllMocks());

describe("paper metadata editor", () => {
  it("saves supported changed fields with the current version and refreshes the detail cache", async () => {
    const result: Asset = { ...asset, title: "New title", row_version: 8 };
    const update = vi.spyOn(api, "updateAsset").mockResolvedValue(result);
    const { client, close } = setup();
    const values: Array<[string, string]> = [
      ["题名 *", "New title"], ["中文题名", "中文标题"], ["副标题", "Subtitle"],
      ["作者（每行一位）", "Alice\nBob"], ["年份", "2026"], ["会议 / 期刊", "NeurIPS"],
      ["语言", "en"], ["标签（逗号分隔）", "robot, vln"],
      ["来源 URL（每行一个）", "https://example.org/paper\nhttps://example.org/project"],
      ["原始摘要", "Paper abstract"], ["简介 / 导读", "Guide"],
    ];
    for (const [label, value] of values) fireEvent.change(screen.getByLabelText(label), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "保存元数据" }));
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
    expect(update).toHaveBeenCalledWith(asset.id, {
      title: "New title", title_zh: "中文标题", subtitle: "Subtitle", authors: ["Alice", "Bob"],
      year: 2026, venue: "NeurIPS", language: "en", tags: ["robot", "vln"],
      source_urls: ["https://example.org/paper", "https://example.org/project"],
      abstract: "Paper abstract", summary: "Guide",
    }, 7);
    expect(client.getQueryData(["asset", asset.id])).toEqual(result);
  });
  it("sends explicit null and empty lists to clear optional fields", async () => {
    const update = vi.spyOn(api, "updateAsset").mockResolvedValue({ ...asset, row_version: 8 });
    setup();
    for (const label of ["作者（每行一位）", "年份", "会议 / 期刊", "简介 / 导读"]) {
      fireEvent.change(screen.getByLabelText(label), { target: { value: "" } });
    }
    fireEvent.click(screen.getByRole("button", { name: "保存元数据" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(asset.id, { authors: [], year: null, venue: null, summary: null }, 7));
  });
  it("keeps user input after a failed save and retries the same patch", async () => {
    const update = vi.spyOn(api, "updateAsset").mockRejectedValueOnce(new Error("网络中断"))
      .mockResolvedValueOnce({ ...asset, summary: "My guide", row_version: 8 });
    const { close } = setup();
    fireEvent.change(screen.getByLabelText("简介 / 导读"), { target: { value: "My guide" } });
    fireEvent.click(screen.getByRole("button", { name: "保存元数据" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("网络中断");
    expect(close).not.toHaveBeenCalled();
    expect(screen.getByLabelText("简介 / 导读")).toHaveValue("My guide");
    fireEvent.click(screen.getByRole("button", { name: "保存元数据" }));
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
    expect(update).toHaveBeenNthCalledWith(2, asset.id, { summary: "My guide" }, 7);
  });
  it("requires a conflict refresh, keeps edits and retries with the newly read version", async () => {
    const update = vi.spyOn(api, "updateAsset")
      .mockRejectedValueOnce(new ApiError({ status: 412, detail: "版本已变化" }))
      .mockResolvedValueOnce({ ...asset, title: "My title", row_version: 10 });
    vi.spyOn(api, "asset").mockRejectedValueOnce(new Error("暂时离线"))
      .mockResolvedValueOnce({ ...asset, title: "Other title", authors: ["Someone else"], row_version: 9 });
    setup();
    fireEvent.change(screen.getByLabelText("题名 *"), { target: { value: "My title" } });
    fireEvent.click(screen.getByRole("button", { name: "保存元数据" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "保存元数据" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "读取最新版本" }));
    await screen.findByText(/读取失败：暂时离线/);
    expect(screen.getByLabelText("题名 *")).toHaveValue("My title");
    expect(screen.getByRole("button", { name: "保存元数据" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "读取最新版本" }));
    await screen.findByText("Other title");
    expect(screen.getByLabelText("题名 *")).toHaveValue("My title");
    fireEvent.click(screen.getByRole("button", { name: "保存元数据" }));
    await waitFor(() => expect(update).toHaveBeenNthCalledWith(2, asset.id, { title: "My title" }, 9));
  });
  it("does not save a blank title or an asset without a version", () => {
    const update = vi.spyOn(api, "updateAsset");
    setup({ ...asset, row_version: undefined });
    fireEvent.change(screen.getByLabelText("题名 *"), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "保存元数据" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "读取最新版本" })).toBeEnabled();
    expect(update).not.toHaveBeenCalled();
  });
  it("preserves commas within existing author names when adding another author", async () => {
    const update = vi.spyOn(api, "updateAsset").mockResolvedValue({ ...asset, row_version: 8 });
    setup({ ...asset, authors: ["Liu, Junqi", "Zhou, Zihao"] });
    const authors = screen.getByLabelText("作者（每行一位）");
    expect(authors).toHaveValue("Liu, Junqi\nZhou, Zihao");
    fireEvent.change(authors, { target: { value: "Liu, Junqi\nZhou, Zihao\n王明" } });
    fireEvent.click(screen.getByRole("button", { name: "保存元数据" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(asset.id, {
      authors: ["Liu, Junqi", "Zhou, Zihao", "王明"],
    }, 7));
  });
});
