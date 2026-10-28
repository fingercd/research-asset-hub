import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { Asset } from "../api/types";
import { CompareProvider } from "../state/CompareContext";
import { ComparePage } from "./ComparePage";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

describe("ComparePage code export", () => {
  it("shows related local code and includes the same status in Markdown", async () => {
    const assets: Asset[] = [{
      id: "paper", title: "Arxiv Paper", asset_type: "paper",
      source_urls: ["https://arxiv.org/abs/1234.5678"],
      related_repositories: [{ id: "repo", title: "Official Repository" }],
      quick_actions: [{ kind: "code", status: "available", owner_asset_id: "repo" }],
    }, { id: "remote", title: "Remote Paper", asset_type: "paper",
      related_repositories: [{ id: "remote-repo", title: "Remote Repository" }],
      quick_actions: [{ kind: "code", status: "network_required", owner_asset_id: "remote-repo" }],
    }];
    vi.spyOn(api, "comparisons").mockResolvedValue({ items: [{ id: "compare", title: "Saved Comparison", asset_ids: assets.map((a) => a.id), assets }], next_cursor: null });
    vi.spyOn(api, "annotations").mockResolvedValue({ items: [], next_cursor: null });
    const blobs: Blob[] = [];
    vi.stubGlobal("URL", class extends URL {
      static createObjectURL(blob: Blob) { blobs.push(blob); return "blob:comparison"; }
      static revokeObjectURL() {}
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter><CompareProvider><ComparePage /></CompareProvider></MemoryRouter>
    </QueryClientProvider>);
    fireEvent.click(await screen.findByText("Saved Comparison"));
    expect(await screen.findByText("本地可读：Official Repository")).toBeInTheDocument();
    expect(screen.getByText("远程代码，需联网：Remote Repository")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "导出 Markdown" }));
    await waitFor(() => expect(blobs).toHaveLength(1));
    const content = await new Promise<string>((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.readAsText(blobs[0]!);
    });
    expect(content).toContain("| 代码 | 本地可读：Official Repository | 远程代码，需联网：Remote Repository |");
  });
});
