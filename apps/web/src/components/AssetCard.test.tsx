import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CompareProvider } from "../state/CompareContext";
import { AssetCard } from "./AssetCard";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("AssetCard", () => {
  it("shows reading state, annotation, and all four quick actions", () => {
    window.localStorage.clear();
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <CompareProvider>
            <AssetCard
              asset={{
                id: "paper-1",
                asset_type: "paper",
                title: "World Models",
                authors: ["A. Researcher"],
                reading_status: "reading",
                annotation_summary: "重要方法笔记",
                representations: [
                  { id: "pdf-1", representation_type: "pdf", open_url: "/pdf" },
                  {
                    id: "html-missing",
                    representation_type: "html",
                    availability: "missing",
                    open_url: "/missing-html",
                  },
                ],
              }}
            />
          </CompareProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getByRole("heading", { name: "World Models" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "更多" }));
    expect(screen.getByLabelText("阅读状态")).toHaveValue("reading");
    expect(screen.getByText("重要方法笔记")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "PDF" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "HTML" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "导读" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "代码" })).toBeDisabled();
  });

  it("copies the asset id without showing it", async () => {
    window.localStorage.clear();
    const clipboard = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: clipboard },
      configurable: true,
    });
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <CompareProvider>
            <AssetCard
              asset={{ id: "paper-copy-1", asset_type: "paper", title: "Copy Paper" }}
            />
          </CompareProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "更多" }));
    fireEvent.click(screen.getByRole("button", { name: "复制ID" }));

    expect(clipboard).toHaveBeenCalledWith("paper-copy-1");
    expect(screen.queryByText("paper-copy-1")).not.toBeInTheDocument();
  });

  it("does not render an empty annotation row", () => {
    window.localStorage.clear();
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <CompareProvider>
            <AssetCard
              asset={{ id: "paper-2", asset_type: "paper", title: "No Notes" }}
            />
          </CompareProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.queryByText("无")).toBeNull();
  });

  it("never offers paper comparison for a repository", () => {
    window.localStorage.clear();
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <CompareProvider>
            <AssetCard
              asset={{ id: "repo-1", asset_type: "repository", title: "Repo" }}
            />
          </CompareProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "更多" }));
    expect(screen.queryByRole("button", { name: "加入对比" })).toBeNull();
  });
});
