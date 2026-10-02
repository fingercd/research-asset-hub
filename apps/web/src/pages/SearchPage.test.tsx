import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { CompareProvider } from "../state/CompareContext";
import { SearchPage } from "./SearchPage";

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-search">{location.search}</output>;
}

function renderSearch(route = "/search") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <CompareProvider>
          <SearchPage />
          <LocationProbe />
        </CompareProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SearchPage", () => {
  it("removes the retired guide filter from legacy deep links", async () => {
    vi.spyOn(api, "domains").mockResolvedValue({
      items: [],
      next_cursor: null,
      total: 0,
    });
    vi.spyOn(api, "search").mockResolvedValue({
      items: [],
      next_cursor: null,
      total: 0,
    });

    renderSearch("/search?q=agent&has_guide=true");

    await waitFor(() =>
      expect(screen.getByTestId("location-search")).toHaveTextContent("?q=agent"),
    );
    expect(screen.getByTestId("location-search")).not.toHaveTextContent(
      "has_guide",
    );
  });

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

  it("keeps the date range and date-first sorting in the common filters", async () => {
    vi.spyOn(api, "domains").mockResolvedValue({
      items: [],
      next_cursor: null,
      total: 0,
    });
    const search = vi.spyOn(api, "search").mockResolvedValue({
      items: [],
      next_cursor: null,
      total: 0,
    });

    renderSearch(
      "/search?date_from=2026-01-02&date_to=2026-08-31",
    );

    const dateFrom = screen.getByLabelText("起始日期");
    const dateTo = screen.getByLabelText("结束日期");
    expect(dateFrom).toHaveAttribute("type", "date");
    expect(dateTo).toHaveAttribute("type", "date");
    expect(dateFrom.closest("details")).toBeNull();
    expect(dateTo.closest("details")).toBeNull();
    expect(screen.getByLabelText("排序")).toHaveValue("date_desc");
    await waitFor(() =>
      expect(search).toHaveBeenCalledWith(
        expect.objectContaining({
          mode: "keyword",
          sort: "date_desc",
          date_from: "2026-01-02",
          date_to: "2026-08-31",
        }),
        expect.any(AbortSignal),
      ),
    );

    fireEvent.change(screen.getByLabelText("排序"), {
      target: { value: "relevance" },
    });
    await waitFor(() =>
      expect(screen.getByTestId("location-search")).toHaveTextContent(
        "sort=relevance",
      ),
    );
    await waitFor(() =>
      expect(search).toHaveBeenLastCalledWith(
        expect.objectContaining({ sort: "relevance" }),
        expect.any(AbortSignal),
      ),
    );
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
    fireEvent.click(screen.getByRole("button", { name: "更多" }));
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
