import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

beforeEach(() => {
  vi.spyOn(api, "collections").mockResolvedValue({items: [], next_cursor: null, total: 0});
  vi.spyOn(api, "savedSearches").mockResolvedValue({items: [], next_cursor: null, total: 0});
});

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

  it("keeps date filtering separate from publication year and collapses secondary controls", async () => {
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
    expect(dateFrom.closest("details")).not.toHaveAttribute("open");
    expect(dateTo.closest("details")).not.toHaveAttribute("open");
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
  it("passes exact venue/year through pagination and removes only the selected filter", async () => {
    vi.spyOn(api, "domains").mockResolvedValue({items: [], next_cursor: null});
    const search = vi.spyOn(api, "search").mockResolvedValue({
      items: [{id: "paper-1", title: "Exact conference paper", asset_type: "paper"}], next_cursor: "page-2", total: 2,
    });
    renderSearch("/search?q=agent&venue=CVPR&year=2025&has_pdf=true");
    await screen.findByRole("heading", {name: "Exact conference paper"});
    expect(screen.getByLabelText("会议或期刊")).toHaveValue("CVPR");
    expect(screen.getByLabelText("发表年份")).toHaveValue(2025);
    fireEvent.click(screen.getByRole("button", {name: "加载更多"}));
    await waitFor(() => expect(search).toHaveBeenLastCalledWith(expect.objectContaining({
      q: "agent", venue: "CVPR", year: 2025, has_pdf: true, cursor: "page-2",
    }), expect.any(AbortSignal)));
    fireEvent.click(screen.getByRole("button", {name: "清除会议/期刊：CVPR"}));
    await waitFor(() => expect(search).toHaveBeenLastCalledWith(expect.objectContaining({
      q: "agent", venue: undefined, year: 2025, has_pdf: true, cursor: undefined,
    }), expect.any(AbortSignal)));
    expect(screen.getByTestId("location-search")).not.toHaveTextContent("venue=");
    expect(screen.getByTestId("location-search")).toHaveTextContent("year=2025");
    fireEvent.click(screen.getByRole("button", {name: "清除全部筛选"}));
    await waitFor(() => expect(screen.getByTestId("location-search").textContent).toBe(""));
  });

  it("submits exact text fields once instead of fetching on every keystroke", async () => {
    vi.spyOn(api, "domains").mockResolvedValue({items: [], next_cursor: null});
    const search = vi.spyOn(api, "search").mockResolvedValue({items: [], next_cursor: null});
    renderSearch();
    await screen.findByText("没有找到匹配资产");
    const calls = search.mock.calls.length;
    fireEvent.change(screen.getByLabelText("会议或期刊"), {target: {value: "ICLR"}});
    fireEvent.change(screen.getByLabelText("发表年份"), {target: {value: "2026"}});
    expect(search).toHaveBeenCalledTimes(calls);
    fireEvent.click(screen.getByRole("button", {name: "搜索"}));
    await waitFor(() => expect(search).toHaveBeenCalledTimes(calls + 1));
    expect(search).toHaveBeenLastCalledWith(expect.objectContaining({venue: "ICLR", year: 2026}), expect.any(AbortSignal));
  });

  it("changes density without a fetch or lost selection and clears selection when filters change", async () => {
    vi.spyOn(api, "domains").mockResolvedValue({items: [], next_cursor: null});
    const search = vi.spyOn(api, "search").mockResolvedValue({items: [{id: "paper-1", title: "Density paper", asset_type: "paper"}], next_cursor: null, total: 1});
    renderSearch();
    await screen.findByRole("heading", {name: "Density paper"});
    fireEvent.click(screen.getByLabelText("选择 Density paper"));
    expect(screen.getByText("已选择 1 项")).toBeInTheDocument();
    const calls = search.mock.calls.length;
    fireEvent.click(screen.getByRole("button", {name: "详细卡片"}));
    expect(screen.getByRole("button", {name: "详细卡片"})).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("选择 Density paper")).toBeChecked();
    fireEvent.click(screen.getByRole("button", {name: "紧凑列表"}));
    expect(search).toHaveBeenCalledTimes(calls);
    expect(screen.getByLabelText("选择 Density paper")).toBeChecked();
    fireEvent.change(screen.getByLabelText("资产类型"), {target: {value: "paper"}});
    await waitFor(() => expect(screen.getByLabelText("选择 Density paper")).not.toBeChecked());
  });

  it("retains loaded results and selection after next-page failure and retries only that page", async () => {
    vi.spyOn(api, "domains").mockResolvedValue({items: [], next_cursor: null});
    const search = vi.spyOn(api, "search")
      .mockResolvedValueOnce({items: [{id: "paper-1", title: "Keep this paper", asset_type: "paper"}], next_cursor: "page-2", total: 2})
      .mockRejectedValueOnce(new Error("page failed"))
      .mockResolvedValueOnce({items: [{id: "paper-2", title: "Next page paper", asset_type: "paper"}], next_cursor: null, total: 2});
    renderSearch("/search?venue=CVPR&year=2025");
    await screen.findByRole("heading", {name: "Keep this paper"});
    fireEvent.click(screen.getByLabelText("选择 Keep this paper"));
    fireEvent.click(screen.getByRole("button", {name: "加载更多"}));
    const retry = await screen.findByRole("button", {name: "重试加载更多"});
    expect(screen.getByRole("heading", {name: "Keep this paper"})).toBeInTheDocument();
    expect(screen.getByLabelText("选择 Keep this paper")).toBeChecked();
    fireEvent.click(retry);
    await screen.findByRole("heading", {name: "Next page paper"});
    expect(search.mock.calls.map(([params]) => params.cursor)).toEqual([undefined, "page-2", "page-2"]);
    expect(screen.getByLabelText("选择 Keep this paper")).toBeChecked();
  });

  it("does not convert publication-year ranges to first-public-date constraints", async () => {
    vi.spyOn(api, "domains").mockResolvedValue({items: [], next_cursor: null});
    const search = vi.spyOn(api, "search").mockResolvedValue({items: [], next_cursor: null});
    renderSearch("/search?year_from=2020&year_to=2025");
    await waitFor(() => expect(search).toHaveBeenCalledWith(expect.objectContaining({year_from: 2020, year_to: 2025, date_from: undefined, date_to: undefined}), expect.any(AbortSignal)));
    expect(screen.getByRole("button", {name: "清除起始年份：2020"})).toBeInTheDocument();
  });

  it("preserves negative availability conditions restored from saved-view URLs", async () => {
    vi.spyOn(api, "domains").mockResolvedValue({items: [], next_cursor: null});
    const search = vi.spyOn(api, "search").mockResolvedValue({items: [], next_cursor: null});
    renderSearch("/search?has_pdf=false&has_code=false&has_html=true&has_annotation=false");
    await waitFor(() => expect(search).toHaveBeenCalledWith(expect.objectContaining({has_pdf: false, has_code: false, has_html: true, has_annotation: false}), expect.any(AbortSignal)));
    expect(screen.getByRole("button", {name: "清除无 PDF"})).toBeInTheDocument();
    expect(screen.getByRole("button", {name: "清除无代码"})).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: "清除无 PDF"}));
    await waitFor(() => expect(search).toHaveBeenLastCalledWith(expect.objectContaining({has_pdf: undefined, has_code: false, has_html: true, has_annotation: false}), expect.any(AbortSignal)));
  });

});
