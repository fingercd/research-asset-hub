import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type {
  Domain,
  DomainPortal,
  ResearchUnitSummary,
} from "../api/types";
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

function portal(): DomainPortal {
  const section = (
    id: DomainPortal["sections"][number]["id"],
    content: Record<string, unknown> = {},
  ): DomainPortal["sections"][number] => ({
    id,
    kind: id.replaceAll("-", "_"),
    title: {
      scope: "领域定位与边界",
      timeline: "发展时间线",
      "capability-method-map": "能力与方法版图",
      landmarks: "代表工作与技术路线",
      benchmarks: "任务、数据集与评测",
      "reading-routes": "推荐阅读路线",
      catalog: "论文、代码与导读目录",
      frontiers: "前沿趋势与开放问题",
      evidence: "证据、来源与版本说明",
    }[id],
    status: "reviewed",
    content,
  });
  return {
    schema: "research-asset-hub.domain-portal/v2",
    slug: "robotics",
    label: "机器人",
    description: "机器人领域统一总览。",
    content_version: "2026.07.26",
    content_digest: "a".repeat(64),
    review_status: "reviewed",
    stale: false,
    stale_reasons: [],
    original_selector: {
      source_id: "domain-portals-20260726",
      source_record_id: "overview:robotics",
    },
    sections: [
      section("scope", { markdown: "机器人领域范围。" }),
      section("catalog", { summary: "正式库动态生成完整目录。" }),
      section("timeline", { stages: [] }),
      section("capability-method-map", {
        capabilities: [],
        methods: [],
        cells: [],
      }),
      section("landmarks", { items: [] }),
      section("benchmarks", { rows: [] }),
      section("reading-routes", { routes: [] }),
      section("frontiers", { markdown: "开放问题。" }),
      section("evidence", { source_inputs: [] }),
    ],
  };
}

function portalResponse(domain: Domain) {
  return {
    schema: "research-asset-hub.domain-portal-response/v2" as const,
    renderer: "v2" as const,
    domain,
    portal: portal(),
  };
}

function renderDomain() {
  vi.spyOn(api,"domain").mockResolvedValue({slug:"robotics",label:"机器人"});
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
    vi.spyOn(api, "domainPortal").mockResolvedValue(
      portalResponse({
        slug: "robotics",
        label: "机器人",
        asset_count: 3,
        recent_assets: [
          { id: "recent-only", title: "Recent preview must not replace the list" },
        ],
      }),
    );
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

    fireEvent.click(await screen.findByRole("tab", { name: "研究单元" }, {timeout:5000}));
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
        element.textContent?.includes("已加载 1 篇论文") === true,
    );
    expect(firstSummary).toHaveTextContent("关联覆盖 2 / 3 项底层资产");

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
        element.textContent?.includes("已加载 2 篇论文") === true,
    );
    expect(secondSummary).toHaveTextContent("关联覆盖 3 / 3 项底层资产");
    expect(screen.queryByRole("button", { name: "加载更多" })).toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "最近更新" }));
    expect(screen.getByRole("heading", { name: "最近更新" })).toBeInTheDocument();
    expect(screen.getByText("Recent preview must not replace the list")).toBeInTheDocument();
    expect(screen.getByText(/展示最近 5 项/)).toBeInTheDocument();
  });

  it("renders the native portal and opens original HTML only on demand", async () => {
    vi.spyOn(api, "domainPortal").mockResolvedValue(
      portalResponse({
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
      }),
    );
    vi.spyOn(api, "search").mockResolvedValue({
      items: [unit("one", [])],
      next_cursor: null,
      total: 1,
    });
    const open = vi
      .spyOn(api, "openRepresentation")
      .mockImplementation(async (_id, options) => ({
        kind: "gateway_session",
        url: "http://127.0.0.1:8766/session/overview-html?presentation=original",
        representation_id: "overview-html",
        presentation: options?.presentation ?? "original",
        embed: options?.embed,
      }));

    renderDomain();

    const overviewTab = await screen.findByRole("tab", { name: "领域总览" }, {timeout:5000});
    expect(overviewTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("article")).toHaveAttribute("data-domain-portal");
    expect(screen.queryByTitle("机器人领域总览")).toBeNull();
    expect(screen.getByText("论文（主口径）")).toBeInTheDocument();
    expect(screen.getByText("代码仓库（单独统计）")).toBeInTheDocument();
    expect(screen.queryByText("研究单元（聚合视图）")).not.toBeInTheDocument();
    expect(screen.queryByText(/与论文一一对应，非额外论文/)).not.toBeInTheDocument();
    const scopeHeading = screen.getByRole("heading", { name: "领域定位与边界" });
    const catalogHeading = screen.getByRole("heading", { name: "论文、代码与导读目录" });
    const timelineHeading = screen.getByRole("heading", { name: "发展时间线" });
    expect(scopeHeading.compareDocumentPosition(catalogHeading)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(catalogHeading.compareDocumentPosition(timelineHeading)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );

    const target = { location: { href: "" }, opener: window };
    vi.spyOn(window, "open").mockReturnValue(target as unknown as Window);
    fireEvent.click(screen.getByRole("button", { name: "原始 HTML" }));
    await waitFor(() =>
      expect(open).toHaveBeenLastCalledWith("overview-html", {
        presentation: "original",
        embed: false,
      }),
    );
    expect(target.location.href).toContain("presentation=original");

    fireEvent.click(screen.getByRole("tab", { name: "研究单元" }));
    expect(
      screen.getByRole("heading", { name: "领域研究单元" }),
    ).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Paper one" })).toBeInTheDocument();
  });
});
