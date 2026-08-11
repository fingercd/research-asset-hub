import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { CompareProvider } from "../state/CompareContext";
import { SearchPage } from "./SearchPage";

function renderSearch(route = "/search") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <CompareProvider>
          <SearchPage />
        </CompareProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SearchPage", () => {
  it("browses a selected domain with an empty query and paper grouping", async () => {
    vi.spyOn(api, "domains").mockResolvedValue({
      items: [{ slug: "robotics", label: "机器人" }],
      next_cursor: null,
      total: 1,
    });
    const search = vi.spyOn(api, "search").mockResolvedValue({
      items: [
        {
          unit_id: "paper:1",
          unit_kind: "paper",
          primary_asset: {
            id: "paper-1",
            asset_type: "paper",
            title: "Browseable Paper",
          },
          related_repositories: [],
          matched_assets: [],
          covered_asset_count: 1,
          match_source: "browse",
          quick_actions: [],
        },
      ],
      next_cursor: null,
      total: 1,
    });

    renderSearch("/search?domain=robotics");

    expect(
      await screen.findByRole("heading", { name: "Browseable Paper" }),
    ).toBeInTheDocument();
    expect(search).toHaveBeenCalledWith(
      expect.objectContaining({
        q: "",
        domain: "robotics",
        group_by: "paper",
      }),
      expect.any(AbortSignal),
    );
    expect(screen.getByText(/全部资产 · 关键词检索/)).toBeInTheDocument();
  });

  it("switches repository searches to ungrouped results", async () => {
    vi.spyOn(api, "domains").mockResolvedValue({
      items: [],
      next_cursor: null,
      total: 0,
    });
    const search = vi.spyOn(api, "search").mockResolvedValue({
      items: [
        {
          id: "repo-1",
          asset_type: "repository",
          title: "Repository Result",
          representations: [
            {
              id: "code-1",
              representation_type: "repository",
              availability: "available",
            },
          ],
        },
      ],
      next_cursor: null,
      total: 1,
    });

    renderSearch("/search?type=repository");

    expect(
      await screen.findByRole("heading", { name: "Repository Result" }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(search).toHaveBeenCalledWith(
        expect.objectContaining({
          q: "",
          type: "repository",
          group_by: "none",
        }),
        expect.any(AbortSignal),
      ),
    );
    const actions = screen.getByLabelText("快速打开");
    expect(actions).toHaveTextContent("代码");
    expect(actions).not.toHaveTextContent("PDF");
    expect(actions).not.toHaveTextContent("HTML");

    fireEvent.change(screen.getByLabelText("资产类型"), {
      target: { value: "" },
    });
    await waitFor(() =>
      expect(search).toHaveBeenLastCalledWith(
        expect.objectContaining({ group_by: "paper" }),
        expect.any(AbortSignal),
      ),
    );
  });
});
