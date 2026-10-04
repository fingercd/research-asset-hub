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
});
