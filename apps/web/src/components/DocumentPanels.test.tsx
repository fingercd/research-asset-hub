import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { Representation } from "../api/types";
import { GuidePanel } from "./DocumentPanels";

const guide: Representation = {
  id: "guide-1",
  representation_type: "markdown",
  mime_type: "text/markdown",
  label: "Safety guide",
};

function renderGuide() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <GuidePanel representation={guide} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("GuidePanel", () => {
  it("shows an upstream session error and retries into the safe Markdown view", async () => {
    const openRepresentation = vi
      .spyOn(api, "openRepresentation")
      .mockRejectedValueOnce(new Error("gateway session unavailable"))
      .mockResolvedValueOnce({ url: "/session/guide-1" });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: async () =>
        '# Safe Guide\n\n<img src="x" onerror="alert(1)" />',
    });
    vi.stubGlobal("fetch", fetchMock);

    renderGuide();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "gateway session unavailable",
    );
    expect(screen.getByRole("button", { name: "重新加载" })).toBeEnabled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));

    expect(await screen.findByRole("heading", { name: "Safe Guide" })).toBeInTheDocument();
    expect(openRepresentation).toHaveBeenCalledTimes(2);
    expect(openRepresentation).toHaveBeenCalledWith("guide-1");
    expect(fetchMock).toHaveBeenCalledWith(
      "/session/guide-1",
      expect.objectContaining({
        credentials: "include",
        headers: { Accept: "text/markdown,text/plain;q=0.9" },
      }),
    );
    expect(document.querySelector(".markdown-view img")).toBeNull();
  });
});
