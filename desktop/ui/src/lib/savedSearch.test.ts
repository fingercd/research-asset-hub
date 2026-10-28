import { describe, expect, it } from "vitest";
import {
  normalizeSearchDefinition,
  searchDefinitionToUrl,
} from "./savedSearch";
describe("saved search definitions", () => {
  it("keeps typed filters and omits pagination", () => {
    expect(
      normalizeSearchDefinition({
        q: "attention",
        has_pdf: "true",
        has_code: "false",
        year_from: "2020",
        cursor: "next",
      }),
    ).toEqual({
      q: "attention",
      has_pdf: true,
      has_code: false,
      year_from: 2020,
    });
  });
  it("restores encoded filters as a usable search URL", () => {
    expect(
      searchDefinitionToUrl({
        q: "视觉 导航",
        has_pdf: true,
        year_from: 2020,
        domain: null,
      }),
    ).toBe(
      "/search?q=%E8%A7%86%E8%A7%89+%E5%AF%BC%E8%88%AA&has_pdf=true&year_from=2020",
    );
  });
  it("round-trips exact venue/year and every active field without a cursor", () => {
    const definition = {q: "视觉 agent", venue: "CVPR", year: "2025", year_from: "2020", date_to: "2026-01-01", mode: "exact", sort: "relevance", domain: "robotics", collection_id: "c1", type: "paper", author: "Li", tag: "review", status: "reading", has_pdf: "true", has_html: "true", has_code: "false", has_annotation: "true", cursor: "next", has_guide: "true"};
    const normalized = normalizeSearchDefinition(definition);
    const restored = Object.fromEntries(new URLSearchParams(searchDefinitionToUrl(definition).split("?")[1]));
    expect(normalizeSearchDefinition(restored)).toEqual(normalized);
    expect(normalized.year).toBe(2025);
    expect(normalized.venue).toBe("CVPR");
    expect(normalized).not.toHaveProperty("cursor");
    expect(normalized).not.toHaveProperty("has_guide");
    expect(normalizeSearchDefinition({year: "", venue: null})).toEqual({});
  });

});
