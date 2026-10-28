import { describe, expect, it } from "vitest";
import type { Representation } from "../api/types";
import { preferGuide } from "./preferredGuide";

const legacy: Representation = {
  id: "legacy-analysis",
  representation_type: "analysis",
  availability: "available",
};

const unified: Representation = {
  id: "unified-html",
  representation_type: "html",
  role: "guide",
  availability: "available",
  attributes: { unified_guide: true },
};

describe("preferGuide", () => {
  it("selects the unified HTML even when legacy analysis appears first", () => {
    expect(preferGuide([legacy, unified])).toBe(unified);
  });

  it("never selects original HTML as a guide, even with a unified flag", () => {
    const original = { ...unified, id: "original-html", role: "original" };
    expect(preferGuide([original, legacy])).toBe(legacy);
    expect(preferGuide([original])).toBeUndefined();
  });

  it("falls back to the first available older guide when unified HTML is missing", () => {
    const missing = { ...unified, availability: "missing" };
    const markdown = { ...legacy, id: "markdown", representation_type: "markdown" };
    expect(preferGuide([missing, markdown, legacy])).toBe(markdown);
  });

  it("excludes every representation without available status", () => {
    const unavailable = ["missing", "blocked", "pending", undefined].map(
      (availability) => ({ ...unified, availability }),
    );
    expect(preferGuide(unavailable)).toBeUndefined();
    expect(preferGuide([])).toBeUndefined();
  });
});
