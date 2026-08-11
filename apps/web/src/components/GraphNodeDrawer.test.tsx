import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { GraphNode } from "../api/types";
import { CompareProvider } from "../state/CompareContext";
import { GraphNodeDrawer } from "./GraphNodeDrawer";

function renderDrawer(node: GraphNode, onClose = vi.fn()) {
  window.localStorage.clear();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <CompareProvider>
          <GraphNodeDrawer node={node} onClose={onClose} />
        </CompareProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("GraphNodeDrawer", () => {
  it("shows asset details and one-hop relation count", async () => {
    vi.spyOn(api, "asset").mockResolvedValue({
      id: "paper-1",
      title: "Drawer Paper",
      authors: ["A. Researcher"],
      year: 2026,
      venue: "CVPR",
      summary: "论文摘要",
      annotation_summary: "重要批注",
      domain_slugs: ["agent"],
      reading_status: "reading",
    });
    vi.spyOn(api, "graphNeighbors").mockResolvedValue({
      nodes: [
        { id: "paper-1", title: "Drawer Paper" },
        { id: "paper-2", title: "Related Paper" },
      ],
      edges: [],
    });

    renderDrawer({
      id: "paper-1",
      title: "Drawer Paper",
      asset_type: "paper",
    });

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Drawer Paper" }),
      ).toBeInTheDocument(),
    );
    await waitFor(() => expect(screen.getByText("论文摘要")).toBeInTheDocument());
    expect(screen.getByText("重要批注")).toBeInTheDocument();
    expect(screen.getByText("一跳关系（1）")).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: /Related Paper/ }),
      ).toHaveAttribute("href", "/assets/paper-2"),
    );
  });

  it("toggles the asset into compare and closes", async () => {
    vi.spyOn(api, "asset").mockResolvedValue({
      id: "paper-3",
      title: "Compare Paper",
      asset_type: "paper",
    });
    vi.spyOn(api, "graphNeighbors").mockResolvedValue({ nodes: [], edges: [] });
    const onClose = vi.fn();

    renderDrawer({ id: "paper-3", title: "Compare Paper" }, onClose);

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "加入对比" }),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "加入对比" }));
    expect(screen.getByRole("button", { name: "已加入对比" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "关闭节点详情" }));
    expect(onClose).toHaveBeenCalledOnce();
  });
});
