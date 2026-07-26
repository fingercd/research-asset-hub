import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { HomePage } from "./HomePage";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("HomePage", () => {
  it("renders every dynamic domain without truncating the research map", async () => {
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
          <HomePage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(
      await screen.findByRole("heading", { name: "研究方向" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "领域 1" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "领域 8" })).toBeInTheDocument();
    expect(screen.getAllByText("进入领域")).toHaveLength(8);
    expect(screen.getByText("总览可用")).toBeInTheDocument();
  });
});
