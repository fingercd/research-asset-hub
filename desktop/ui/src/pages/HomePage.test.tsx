import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { CompareProvider } from "../state/CompareContext";
import { HomePage } from "./HomePage";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("HomePage", () => {
  it("continues available PDFs in one step without overriding the saved page; non-PDF cards stay ordinary links", async () => {
    vi.spyOn(api, "dashboard").mockResolvedValue({
      continue_reading: [
        { id: "pdf-paper", title: "PDF Paper", asset_type: "paper", representations: [{ id: "pdf", representation_type: "pdf", availability: "available" }] },
        { id: "no-pdf", title: "No PDF", asset_type: "paper", representations: [{ id: "missing", representation_type: "pdf", availability: "missing" }] },
      ], recent_assets: [], domains: [],
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter><CompareProvider><HomePage /></CompareProvider></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByRole("link", { name: "继续阅读 PDF" })).toHaveAttribute("href", "/assets/pdf-paper?view=pdf");
    expect(screen.getByRole("link", { name: "查看资料" })).toHaveAttribute("href", "/assets/pdf-paper");
    expect(screen.getByRole("link", { name: /打开资料/ })).toHaveAttribute("href", "/assets/no-pdf");
  });
  it("renders one search entry, compact metrics, and five frequent domains", async () => {
    vi.spyOn(api, "dashboard").mockResolvedValue({
      stats: {
        assets: 80,
        papers: 64,
        unread: 60,
        reading: 4,
        read: 16,
        domains: 8,
        relations: 20,
        annotations: 3,
      },
      recent_assets: [],
      domains: Array.from({ length: 8 }, (_, index) => ({
        slug: `domain-${index + 1}`,
        label: `领域 ${index + 1}`,
        description: `第 ${index + 1} 个动态领域`,
        paper_count: index + 1,
        repository_count: index,
        research_unit_count: index + 1,
        overview_status: index === 7 ? "available" : "unavailable",
        reading_counts: { reading: index % 2 },
      })),
    });

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <CompareProvider>
            <HomePage />
          </CompareProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(
      await screen.findByRole("heading", { name: "常用领域" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("searchbox")).toHaveLength(1);
    expect(screen.getByRole("link", { name: /领域 1/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /领域 5/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /领域 6/ })).toBeNull();
    expect(screen.getByRole("link", { name: /查看全部领域/ })).toHaveAttribute(
      "href",
      "/domains",
    );
    expect(screen.getByText("64")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("does not repeat continuing-reading assets in the recent section", async () => {
    const continuingAsset = {
      id: "paper-1",
      asset_type: "paper",
      title: "Same Paper",
      reading_status: "reading" as const,
    };
    vi.spyOn(api, "dashboard").mockResolvedValue({
      stats: {
        assets: 1,
        papers: 1,
        unread: 0,
        reading: 1,
        read: 0,
        domains: 0,
        relations: 0,
        annotations: 0,
      },
      continue_reading: [continuingAsset],
      recent_assets: [continuingAsset],
      domains: [],
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <CompareProvider>
            <HomePage />
          </CompareProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(
      await screen.findByRole("heading", { name: "Same Paper" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { name: "Same Paper" })).toHaveLength(1);
    expect(screen.getByText("没有其他最近资料")).toBeInTheDocument();
  });
});
