import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { JobsPage } from "./JobsPage";

function renderJobs() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <JobsPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("JobsPage", () => {
  it("loads the second page and refreshes all loaded pages without hiding history", async () => {
    let refreshed = false;
    const jobs = vi.spyOn(api, "jobs").mockImplementation(async (_signal, cursor) => ({
      items: [{ id: cursor ? "old-job" : "new-job", name: cursor ? "历史任务" : "最新任务", status: refreshed ? "succeeded" : "running" }],
      next_cursor: cursor ? null : "page-2",
    }));
    renderJobs();
    await screen.findByText("最新任务");
    fireEvent.click(screen.getByRole("button", { name: "加载更多任务" }));
    await screen.findByText("历史任务");
    expect(jobs).toHaveBeenLastCalledWith(expect.any(AbortSignal), "page-2");
    expect(screen.queryByRole("button", { name: "加载更多任务" })).not.toBeInTheDocument();
    refreshed = true;
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    await waitFor(() => expect(screen.getAllByText("已完成")).toHaveLength(2));
    expect(jobs).toHaveBeenCalledTimes(4);
    expect(screen.getByText("历史任务")).toBeInTheDocument();
  });

  it("keeps loaded jobs visible when loading more fails and can retry the same cursor", async () => {
    const jobs = vi.spyOn(api, "jobs")
      .mockResolvedValueOnce({ items: [{ id: "job-1", name: "已加载任务" }], next_cursor: "page-2" })
      .mockRejectedValueOnce(new Error("历史任务加载失败"))
      .mockResolvedValueOnce({ items: [{ id: "job-2", name: "下一页任务" }], next_cursor: null });
    renderJobs();
    await screen.findByText("已加载任务");
    fireEvent.click(screen.getByRole("button", { name: "加载更多任务" }));
    await screen.findByText("历史任务加载失败");
    expect(screen.getByText("已加载任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await screen.findByText("下一页任务");
    expect(jobs).toHaveBeenLastCalledWith(expect.any(AbortSignal), "page-2");
  });

  it("automatically refreshes active jobs on a later loaded page", async () => {
    vi.useFakeTimers();
    const jobs = vi.spyOn(api, "jobs").mockImplementation(async (_signal, cursor) => ({
      items: [{ id: cursor ? "active-job" : "done-job", name: cursor ? "运行中的历史任务" : "已完成任务", status: cursor ? "running" : "succeeded" }],
      next_cursor: cursor ? null : "page-2",
    }));
    renderJobs();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    fireEvent.click(screen.getByRole("button", { name: "加载更多任务" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByText("运行中的历史任务")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(jobs).toHaveBeenCalledTimes(4);
    expect(jobs).toHaveBeenLastCalledWith(expect.any(AbortSignal), "page-2");
  });

  it("renders cancelled or superseded reasons as neutral notes while failures stay red", async () => {
    vi.spyOn(api, "jobs").mockResolvedValue({
      items: [
        {
          id: "cancelled-index",
          name: "index_asset",
          status: "cancelled",
          error: "A versioned bulk rebuild superseded this per-entity index job.",
        },
        {
          id: "failed-extract",
          name: "extract_pdf",
          status: "failed",
          error: "Parser crashed.",
        },
      ],
      next_cursor: null,
      total: 2,
    });

    renderJobs();

    expect(
      await screen.findByText(
        "A versioned bulk rebuild superseded this per-entity index job.",
      ),
    ).toHaveClass("job-note");
    expect(screen.getByText("Parser crashed.")).toHaveClass("job-error");
    expect(screen.getByText("已取消")).toBeInTheDocument();
    expect(screen.getByText("失败")).toBeInTheDocument();
  });
});
