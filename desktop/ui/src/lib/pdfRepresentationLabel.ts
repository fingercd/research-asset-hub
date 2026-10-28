import type { Representation } from "../api/types";

/** Version names describe registered evidence, never guessed chronology. */
export function pdfRepresentationLabel(representation: Representation): string {
  const role = representation.role === "primary" ? "主版本"
    : representation.role === "original" ? "原始版本" : representation.role;
  const filename = (representation.entrypoint || representation.path_ref)?.split(/[\\/]/).pop();
  const version = representation.external_url?.match(/v(\d+)(?:[/.?#]|$)/)?.[1];
  const parts = [representation.label || "PDF", role, filename, version && `来源 v${version}`].filter(Boolean);
  if (parts.length === 1) parts.push(`表示 ${representation.id.slice(0, 8)}`);
  return parts.join(" · ");
}
