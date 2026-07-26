import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { JobsPage } from "./JobsPage";

function renderJobs() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <JobsPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("JobsPage", () => {
  it("renders cancelled or superseded reasons as neutral notes while failures stay red", async () => {
    vi.spyOn(api, "jobs").mockResolvedValue({
      items: [
        {
          id: "cancelled-index",
          name: "index_asset",
          status: "cancelled",
          error: "A versioned bulk rebuild superseded this per-entity index job.",
        },
        {
          id: "failed-extract",
          name: "extract_pdf",
          status: "failed",
          error: "Parser crashed.",
        },
      ],
      next_cursor: null,
      total: 2,
    });

    renderJobs();

    expect(
      await screen.findByText(
        "A versioned bulk rebuild superseded this per-entity index job.",
      ),
    ).toHaveClass("job-note");
    expect(screen.getByText("Parser crashed.")).toHaveClass("job-error");
    expect(screen.getByText("已取消")).toBeInTheDocument();
    expect(screen.getByText("失败")).toBeInTheDocument();
  });
});
