import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { ConflictsPage } from "./ConflictsPage";

function renderConflicts() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter><ConflictsPage /></MemoryRouter></QueryClientProvider>);
}

afterEach(() => { vi.restoreAllMocks(); });

describe("ConflictsPage", () => {
  it("loads further pending conflicts and refetches the loaded list after resolution", async () => {
    let resolved = false;
    const conflicts = vi.spyOn(api, "conflicts").mockImplementation(async (_signal, cursor) => ({
      items: resolved
        ? [{ id: cursor ? "c3" : "c2", title: cursor ? "补位冲突" : "第二页冲突", field: "/title", status: "pending", row_version: 1 }]
        : [{ id: cursor ? "c2" : "c1", title: cursor ? "第二页冲突" : "第一页冲突", field: "/title", status: "pending", row_version: 1 }],
      next_cursor: cursor ? null : "page-2",
    }));
    vi.spyOn(api, "resolveConflict").mockImplementation(async () => { resolved = true; return { id: "c1", status: "resolved" }; });
    renderConflicts();
    await screen.findByText("第一页冲突");
    fireEvent.click(screen.getByRole("button", { name: "加载更多冲突" }));
    await screen.findByText("第二页冲突");
    expect(conflicts).toHaveBeenLastCalledWith(expect.any(AbortSignal), "page-2", "pending");
    const firstCard = screen.getByText("第一页冲突").closest("article")!;
    fireEvent.click(within(firstCard).getByRole("button", { name: "保留本地" }));
    await screen.findByText("补位冲突");
    expect(screen.queryByText("第一页冲突")).not.toBeInTheDocument();
    expect(screen.getAllByText("第二页冲突")).toHaveLength(1);
    expect(conflicts).toHaveBeenCalledTimes(4);
  });

  it("keeps a conflict visible and explains failed resolution", async () => {
    vi.spyOn(api, "conflicts").mockResolvedValue({ items: [{ id: "c1", title: "待处理冲突", field: "/title", row_version: 5 }], next_cursor: null });
    vi.spyOn(api, "resolveConflict").mockRejectedValue(new Error("同步状态已变化，请刷新"));
    renderConflicts();
    await screen.findByText("待处理冲突");
    fireEvent.click(screen.getByRole("button", { name: "采用来源" }));
    await screen.findByRole("alert");
    expect(screen.getByText("同步状态已变化，请刷新")).toBeInTheDocument();
    expect(screen.getByText("待处理冲突")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "采用来源" })).toBeEnabled());
  });

  it("removes a resolved conflict even when refreshing the remaining list fails", async () => {
    vi.spyOn(api, "conflicts")
      .mockResolvedValueOnce({ items: [{ id: "c1", title: "已解决的冲突", row_version: 2 }], next_cursor: null })
      .mockRejectedValueOnce(new Error("剩余冲突刷新失败"));
    vi.spyOn(api, "resolveConflict").mockResolvedValue({ id: "c1", status: "resolved" });
    renderConflicts();
    await screen.findByText("已解决的冲突");
    fireEvent.click(screen.getByRole("button", { name: "保留本地" }));
    await screen.findByText("剩余冲突刷新失败");
    expect(screen.queryByText("已解决的冲突")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保留本地" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新加载" })).toBeInTheDocument();
  });

  it.each([
    { field: "/title", label: "合并后的题名", role: "textbox", local: "原题名", input: "合并题名", value: "合并题名" },
    { field: "/authors", label: "合并后的作者", role: "textbox", local: ["原作者"], input: "张三\n李四", value: ["张三", "李四"] },
    { field: "/year", label: "合并后的年份", role: "spinbutton", local: 2024, input: "2025", value: 2025 },
    { field: "/year", label: "合并后的年份", role: "spinbutton", local: 2024, input: "", value: null },
    { field: "/reading_status", label: "合并后的阅读状态", role: "combobox", local: "unread", input: "read", value: "read" },
  ])("confirms a typed merge value for $field before applying it", async ({ field, label, role, local, input, value }) => {
    let resolved = false;
    vi.spyOn(api, "conflicts").mockImplementation(async () => ({
      items: resolved ? [] : [{ id: "merge-1", field, local_value: local, incoming_value: "来源值", row_version: 8 }],
      next_cursor: null,
    }));
    const resolve = vi.spyOn(api, "resolveConflict").mockImplementation(async () => { resolved = true; return { id: "merge-1", status: "resolved" }; });
    renderConflicts();
    fireEvent.click(await screen.findByRole("button", { name: "合并" }));
    expect(resolve).not.toHaveBeenCalled();
    const control = screen.getByRole(role, { name: label });
    fireEvent.change(control, { target: { value: input } });
    fireEvent.click(screen.getByRole("button", { name: "确认合并并同步" }));
    await screen.findByText("没有待处理冲突");
    expect(resolve).toHaveBeenCalledWith("merge-1", "merge", 8, value);
  });

  it("preserves the entered merge value after a failed submission and permits retry", async () => {
    vi.spyOn(api, "conflicts").mockResolvedValue({ items: [{ id: "c1", field: "/summary", local_value: "原简介", row_version: 9 }], next_cursor: null });
    const resolve = vi.spyOn(api, "resolveConflict").mockRejectedValue(new Error("Obsidian 笔记已变化，请重新检查"));
    renderConflicts();
    fireEvent.click(await screen.findByRole("button", { name: "合并" }));
    const input = screen.getByRole("textbox", { name: "合并后的简介" });
    fireEvent.change(input, { target: { value: "用户整理后的简介" } });
    fireEvent.click(screen.getByRole("button", { name: "确认合并并同步" }));
    await screen.findByText("Obsidian 笔记已变化，请重新检查");
    expect(input).toHaveValue("用户整理后的简介");
    expect(screen.getByRole("button", { name: "确认合并并同步" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "确认合并并同步" }));
    await waitFor(() => expect(resolve).toHaveBeenCalledTimes(2));
    expect(resolve).toHaveBeenLastCalledWith("c1", "merge", 9, "用户整理后的简介");
  });

  it("offers only the current Hub value for system managed fields", async () => {
    vi.spyOn(api, "conflicts").mockResolvedValue({ items: [{ id: "c1", field: "/asset_id", row_version: 3 }], next_cursor: null });
    renderConflicts();
    await screen.findByText("这是系统管理字段，只能保留 Hub 值并修正 Obsidian 笔记。");
    expect(screen.queryByRole("button", { name: "合并" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "采用来源" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "保留本地" })).toBeEnabled();
  });
});
