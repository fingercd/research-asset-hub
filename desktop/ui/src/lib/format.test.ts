import { describe, expect, it } from "vitest";
import { annotationSnippet, truncateText } from "./format";

describe("result annotation snippets", () => {
  it("uses 无 when the asset has no annotation", () => {
    expect(annotationSnippet({ id: "paper-1", title: "Paper" })).toBe("无");
  });

  it("limits a Chinese annotation to 60 characters", () => {
    const note = "研".repeat(61);
    const result = annotationSnippet({
      id: "paper-1",
      title: "Paper",
      annotation_summary: note,
    });

    expect(result).toBe(`${"研".repeat(60)}…`);
    expect(Array.from(result).length).toBe(61);
  });

  it("does not split Unicode code points", () => {
    expect(truncateText("📚".repeat(3), 2)).toBe("📚📚…");
  });
});
