import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { CompareProvider, useCompare } from "./CompareContext";

function CompareHarness() {
  const compare = useCompare();
  return (
    <div>
      <output aria-label="已选数量">{compare.assets.length}</output>
      <output aria-label="已选标题">
        {compare.assets.map((asset) => asset.title).join(",")}
      </output>
      <button
        type="button"
        onClick={() => compare.add({ id: "repo-1", title: "Repository", asset_type: "repository" })}
      >
        添加仓库
      </button>
      <button
        type="button"
        onClick={() => compare.toggle({ id: "repo-1", title: "Repository", type: "repository" })}
      >
        切换仓库
      </button>
      <button
        type="button"
        onClick={() => compare.add({ id: "paper-1", title: "Paper", asset_type: "paper" })}
      >
        添加论文
      </button>
    </div>
  );
}

function renderHarness() {
  return render(
    <CompareProvider>
      <CompareHarness />
    </CompareProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
});

describe("CompareProvider", () => {
  it("filters non-paper and duplicate legacy entries from initial state", () => {
    window.localStorage.setItem(
      "rah.compare-assets",
      JSON.stringify([
        { id: "repo-1", title: "Repository", asset_type: "repository" },
        { id: "paper-1", title: "Paper", asset_type: "paper" },
        { id: "paper-1", title: "Duplicate Paper", asset_type: "paper" },
      ]),
    );

    renderHarness();

    expect(screen.getByLabelText("已选数量")).toHaveTextContent("1");
    expect(screen.getByLabelText("已选标题")).toHaveTextContent("Paper");
    expect(screen.getByLabelText("已选标题")).not.toHaveTextContent("Repository");
    expect(screen.getByLabelText("已选标题")).not.toHaveTextContent("Duplicate Paper");
  });

  it("rejects non-paper add and toggle operations while accepting papers", () => {
    renderHarness();

    fireEvent.click(screen.getByRole("button", { name: "添加仓库" }));
    fireEvent.click(screen.getByRole("button", { name: "切换仓库" }));
    expect(screen.getByLabelText("已选数量")).toHaveTextContent("0");
    expect(window.localStorage.getItem("rah.compare-assets")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "添加论文" }));
    expect(screen.getByLabelText("已选数量")).toHaveTextContent("1");
    expect(JSON.parse(window.localStorage.getItem("rah.compare-assets") ?? "[]"))
      .toEqual([expect.objectContaining({ id: "paper-1", asset_type: "paper" })]);
  });
});
