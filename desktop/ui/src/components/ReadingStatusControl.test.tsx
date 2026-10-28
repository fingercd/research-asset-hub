import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { Asset, ReadingStatus } from "../api/types";
import { ReadingStatusControl } from "./ReadingStatusControl";

function renderControl(value: ReadingStatus = "unread") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  queryClient.setQueryData<Asset>(["asset", "asset-1"], {
    id: "asset-1",
    title: "Reading state test",
    reading_status: value,
    reading_state: { status: value },
  });

  const tree = (assetId: string, status: ReadingStatus) => (
    <QueryClientProvider client={queryClient}>
      <ReadingStatusControl assetId={assetId} value={status} />
    </QueryClientProvider>
  );

  return { ...render(tree("asset-1", value)), queryClient, tree };
}

describe("ReadingStatusControl", () => {
  it("reverts a failed save and lets the user retry it", async () => {
    let rejectFirstRequest: ((reason?: unknown) => void) | undefined;
    const update = vi
      .spyOn(api, "updateReadingStatus")
      .mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => {
            rejectFirstRequest = reject;
          }),
      )
      .mockResolvedValueOnce({ status: "read" });
    const view = renderControl();
    const select = screen.getByRole("combobox", { name: "阅读状态" });

    fireEvent.change(select, { target: { value: "read" } });
    await waitFor(() => {
      expect(select).toHaveValue("read");
      expect(select).toBeDisabled();
    });

    await act(async () => {
      rejectFirstRequest?.(new Error("network unavailable"));
    });
    await waitFor(() => expect(select).toHaveValue("unread"));
    expect(select).toBeEnabled();
    expect(screen.getByRole("alert")).toHaveTextContent("保存失败");
    expect(screen.getByRole("button", { name: "重试" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(select).toHaveValue("read"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(update).toHaveBeenNthCalledWith(1, "asset-1", "read");
    expect(update).toHaveBeenNthCalledWith(2, "asset-1", "read");
    expect(view.queryClient.getQueryData<Asset>(["asset", "asset-1"])).toMatchObject({
      reading_status: "read",
      reading_state: { status: "read" },
    });

    view.rerender(view.tree("asset-1", "read"));
    expect(screen.getByRole("combobox", { name: "阅读状态" })).toHaveValue("read");

    view.rerender(view.tree("asset-2", "unread"));
    expect(screen.getByRole("combobox", { name: "阅读状态" })).toHaveValue("unread");
  });
});
