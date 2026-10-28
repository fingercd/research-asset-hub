import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { SavedSearchControls } from "./SavedSearchControls";

function renderControls(definition: Record<string, unknown>) {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}});
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/search#saved-searches"]}><SavedSearchControls definition={definition} /></MemoryRouter></QueryClientProvider>);
}
afterEach(() => vi.restoreAllMocks());

describe("SavedSearchControls", () => {
  it("saves exact venue/year with typed conditions and restores all filters", async () => {
    const saved = {id: "v1", name: "会议阅读", row_version: 2, definition: {q: "视觉 导航", venue: "CVPR", year: 2025, has_pdf: true, status: "reading"}};
    vi.spyOn(api, "savedSearches").mockResolvedValue({items: [saved], next_cursor: null});
    const create = vi.spyOn(api, "createSavedSearch").mockResolvedValue(saved);
    renderControls({q: "视觉 导航", venue: "CVPR", year: "2025", has_pdf: "true", status: "reading", cursor: "next"});
    expect(screen.getByText("保存与管理视图").closest("details")).toHaveAttribute("open");
    expect(await screen.findByRole("link", {name: "会议阅读"})).toHaveAttribute("href", "/search?q=%E8%A7%86%E8%A7%89+%E5%AF%BC%E8%88%AA&venue=CVPR&year=2025&has_pdf=true&status=reading");
    fireEvent.change(screen.getByLabelText("筛选名称"), {target: {value: "新视图"}});
    fireEvent.click(screen.getByRole("button", {name: "保存当前筛选"}));
    await waitFor(() => expect(create).toHaveBeenCalledWith({name: "新视图", definition: saved.definition}));
  });

  it("updates saved views with their row version and surfaces a conflict", async () => {
    const saved = {id: "v1", name: "会议阅读", row_version: 3, definition: {venue: "CVPR"}};
    vi.spyOn(api, "savedSearches").mockResolvedValue({items: [saved], next_cursor: null});
    const update = vi.spyOn(api, "updateSavedSearch").mockRejectedValue(new Error("version conflict"));
    renderControls({venue: "ICLR", year: "2026"});
    fireEvent.click(await screen.findByRole("button", {name: "更新为当前条件"}));
    fireEvent.click(screen.getByRole("button", {name: "用当前条件更新筛选"}));
    await waitFor(() => expect(update).toHaveBeenCalledWith("v1", {name: "会议阅读", definition: {venue: "ICLR", year: 2026}}, 3));
    expect(await screen.findByRole("alert")).toHaveTextContent("version conflict");
  });
});
