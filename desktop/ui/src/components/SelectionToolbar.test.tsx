import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { SelectionToolbar } from "./SelectionToolbar";

function renderToolbar(ids = ["a", "b", "c"]) {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}});
  return render(<QueryClientProvider client={client}><SelectionToolbar ids={ids} clear={vi.fn()} /></QueryClientProvider>);
}
beforeEach(() => {
  vi.spyOn(api, "collections").mockResolvedValue({items: [], next_cursor: null});
  vi.spyOn(api, "domains").mockResolvedValue({items: [], next_cursor: null});
});
afterEach(() => vi.restoreAllMocks());

describe("SelectionToolbar", () => {
  it("retries only failed IDs with the original payload and retains cumulative successes", async () => {
    const bulk = vi.spyOn(api, "bulkAssets")
      .mockResolvedValueOnce({items: [{id: "a", status: "updated"}, {id: "b", status: "failed", error: {code: "write_conflict", message: "reload and retry"}}, {id: "c", status: "unchanged"}], total: 3, succeeded: 2, failed: 1})
      .mockResolvedValueOnce({items: [{id: "b", status: "updated"}], total: 1, succeeded: 1, failed: 0});
    renderToolbar();
    fireEvent.change(screen.getByLabelText("阅读状态"), {target: {value: "read"}});
    fireEvent.change(screen.getByLabelText("批量标签"), {target: {value: "A，B"}});
    fireEvent.click(screen.getByRole("button", {name: "应用批量修改"}));
    await screen.findByText(/成功 2 项 \/ 失败 1 项/);
    expect(screen.getByRole("button", {name: "本批已提交"})).toBeDisabled();
    expect(bulk).toHaveBeenNthCalledWith(1, {asset_ids: ["a", "b", "c"], reading_status: "read", add_tags: ["A", "B"]});
    fireEvent.click(screen.getByRole("button", {name: "仅重试失败 1 项"}));
    await screen.findByText(/成功 3 项 \/ 失败 0 项/);
    expect(bulk).toHaveBeenNthCalledWith(2, {asset_ids: ["b"], reading_status: "read", add_tags: ["A", "B"]});
    expect(screen.queryByRole("button", {name: /仅重试失败/})).not.toBeInTheDocument();
    expect(bulk).toHaveBeenCalledTimes(2);
  });

  it("does not reuse previous failures after operation parameters change", async () => {
    const bulk = vi.spyOn(api, "bulkAssets").mockResolvedValue({items: [{id: "a", status: "updated"}, {id: "b", status: "failed", error: "conflict"}], total: 2, succeeded: 1, failed: 1});
    renderToolbar(["a", "b"]);
    fireEvent.change(screen.getByLabelText("批量标签"), {target: {value: "old"}});
    fireEvent.click(screen.getByRole("button", {name: "应用批量修改"}));
    await screen.findByRole("button", {name: "仅重试失败 1 项"});
    fireEvent.change(screen.getByLabelText("批量标签"), {target: {value: "new"}});
    expect(screen.queryByRole("button", {name: /仅重试失败/})).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: "应用批量修改"}));
    await waitFor(() => expect(bulk).toHaveBeenLastCalledWith({asset_ids: ["a", "b"], add_tags: ["new"]}));
  });

  it("keeps failed-ID retry scope after a retry request itself fails", async () => {
    const bulk = vi.spyOn(api, "bulkAssets")
      .mockResolvedValueOnce({items: [{id: "a", status: "updated"}, {id: "b", status: "failed", error: "conflict"}], total: 2, succeeded: 1, failed: 1})
      .mockRejectedValueOnce(new Error("network interrupted"))
      .mockResolvedValueOnce({items: [{id: "b", status: "unchanged"}], total: 1, succeeded: 1, failed: 0});
    renderToolbar(["a", "b"]);
    fireEvent.change(screen.getByLabelText("阅读状态"), {target: {value: "reading"}});
    fireEvent.click(screen.getByRole("button", {name: "应用批量修改"}));
    fireEvent.click(await screen.findByRole("button", {name: "仅重试失败 1 项"}));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", {name: "仅重试失败 1 项"}));
    await screen.findByText(/成功 2 项 \/ 失败 0 项/);
    expect(bulk.mock.calls.map(([body]) => body.asset_ids)).toEqual([["a", "b"], ["b"], ["b"]]);
  });
});
