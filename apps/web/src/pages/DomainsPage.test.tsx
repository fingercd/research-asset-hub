import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { ResearchUnitSummary } from "../api/types";
import { CompareProvider } from "../state/CompareContext";
import { DomainDetailPage } from "./DomainsPage";

function unit(id: string, repositoryIds: string[]): ResearchUnitSummary {
  return {
    unit_id: id,
    unit_kind: "paper",
    primary_asset: {
      id,
      asset_type: "paper",
      title: `Paper ${id}`,
      domain_slugs: ["robotics"],
    },
    related_repositories: repositoryIds.map((repositoryId) => ({
      id: repositoryId,
      asset_type: "repository",
      title: `Repository ${repositoryId}`,
      domain_slugs: ["robotics"],
    })),
    matched_assets: [],
    covered_asset_count: 1 + repositoryIds.length,
    quick_actions: [],
    match_source: "browse",
  };
}

function renderDomain() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/domains/robotics"]}>
        <CompareProvider>
          <Routes>
            <Route path="/domains/:slug" element={<DomainDetailPage />} />
          </Routes>
        </CompareProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("DomainDetailPage", () => {
  it("uses empty-query aggregation, reports covered assets, and loads more pages", async () => {
    vi.spyOn(api, "domain").mockResolvedValue({
      slug: "robotics",
      label: "机器人",
      asset_count: 3,
      recent_assets: [
        { id: "recent-only", title: "Recent preview must not replace the list" },
      ],
    });
    const search = vi
      .spyOn(api, "search")
      .mockResolvedValueOnce({
        items: [unit("one", ["shared"])],
        next_cursor: "cursor-30",
        total: 2,
      })
      .mockResolvedValueOnce({
        items: [unit("two", ["shared"])],
        next_cursor: null,
        total: 2,
      });

    renderDomain();

    expect(await screen.findByRole("heading", { name: "Paper one" })).toBeInTheDocument();
    expect(search).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({
        q: "",
        domain: "robotics",
        group_by: "paper",
        cursor: undefined,
      }),
      expect.any(AbortSignal),
    );
    const firstSummary = screen.getByText(
      (_content, element) =>
        element?.classList.contains("loaded-unit-summary") === true &&
        element.textContent?.includes("已加载 1 个研究单元") === true,
    );
    expect(firstSummary).toHaveTextContent("覆盖 2 / 3 项资产");

    fireEvent.click(screen.getByRole("button", { name: "加载更多" }));

    expect(await screen.findByRole("heading", { name: "Paper two" })).toBeInTheDocument();
    await waitFor(() =>
      expect(search).toHaveBeenNthCalledWith(
        2,
        expect.objectContaining({ cursor: "cursor-30" }),
        expect.any(AbortSignal),
      ),
    );
    const secondSummary = screen.getByText(
      (_content, element) =>
        element?.classList.contains("loaded-unit-summary") === true &&
        element.textContent?.includes("已加载 2 个研究单元") === true,
    );
    expect(secondSummary).toHaveTextContent("覆盖 3 / 3 项资产");
    expect(screen.queryByRole("button", { name: "加载更多" })).toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "最近更新" }));
    expect(screen.getByRole("heading", { name: "最近更新" })).toBeInTheDocument();
    expect(screen.getByText("Recent preview must not replace the list")).toBeInTheDocument();
    expect(screen.getByText(/展示最近 5 项/)).toBeInTheDocument();
  });

  it("opens the registered overview in hub mode and switches to original presentation", async () => {
    vi.spyOn(api, "domain").mockResolvedValue({
      slug: "robotics",
      label: "机器人",
      asset_count: 1,
      paper_count: 1,
      repository_count: 0,
      research_unit_count: 1,
      overview_status: "available",
      overview: {
        asset_id: "portal",
        representation_id: "overview-html",
        status: "available",
        default_presentation: "hub",
        last_verified_at: "2026-07-26T00:00:00Z",
        warning: null,
      },
    });
    vi.spyOn(api, "search").mockResolvedValue({
      items: [unit("one", [])],
      next_cursor: null,
      total: 1,
    });
    const open = vi
      .spyOn(api, "openRepresentation")
      .mockImplementation(async (_id, options) => ({
        kind: "gateway_session",
        url: `http://127.0.0.1:8766/session/overview-html?presentation=${
          options?.presentation ?? "original"
        }`,
        representation_id: "overview-html",
        presentation: options?.presentation ?? "original",
        embed: options?.embed,
      }));

    renderDomain();

    const overviewTab = await screen.findByRole("tab", { name: "领域总览" });
    expect(overviewTab).toHaveAttribute("aria-selected", "true");
    expect(
      await screen.findByTitle("机器人领域总览"),
    ).toHaveAttribute("src", expect.stringContaining("presentation=hub"));
    expect(open).toHaveBeenCalledWith("overview-html", {
      presentation: "hub",
      embed: true,
    });
    expect(
      screen.getByRole("link", { name: "新窗口打开" }),
    ).toHaveAttribute("target", "_blank");

    fireEvent.click(screen.getByRole("button", { name: "原始样式" }));
    await waitFor(() =>
      expect(open).toHaveBeenLastCalledWith("overview-html", {
        presentation: "original",
        embed: true,
      }),
    );
    expect(await screen.findByTitle("机器人领域总览")).toHaveAttribute(
      "src",
      expect.stringContaining("presentation=original"),
    );

    fireEvent.click(screen.getByRole("tab", { name: "研究单元" }));
    expect(
      screen.getByRole("heading", { name: "领域研究单元" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Paper one" })).toBeInTheDocument();
  });
});
