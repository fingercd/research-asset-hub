import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// The native shell injects this before loading the UI. Tests provide their own
// explicit transport; the product has no browser/proxy fallback.
window.__HUB_API_BASE__ = "/api/v1";

afterEach(() => {
  cleanup();
});
