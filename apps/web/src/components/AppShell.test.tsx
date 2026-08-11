import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { CompareProvider } from "../state/CompareContext";
import { AppShell } from "./AppShell";

function renderShell() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <CompareProvider>
          <AppShell>
            <div>页面内容</div>
          </AppShell>
        </CompareProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AppShell", () => {
  it("opens the global search with Ctrl+K and focuses the search box", async () => {
    vi.spyOn(api, "health").mockRejectedValue(new Error("offline"));
    vi.spyOn(api, "settings").mockResolvedValue({
      app: { gitnexus_url: "http://127.0.0.1:4747" },
    });
    renderShell();

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });

    const dialog = screen.getByRole("dialog", { name: "快速搜索" });
    expect(dialog).toBeInTheDocument();
    expect(await screen.findByRole("searchbox", { name: "全局搜索关键词" })).toHaveFocus();
  });

  it("links to the code graph page from the research navigation", async () => {
    vi.spyOn(api, "health").mockRejectedValue(new Error("offline"));
    renderShell();

    const link = await screen.findByRole("link", { name: "代码图谱" });
    expect(link).toHaveAttribute("href", "/code-graph");
  });
});
