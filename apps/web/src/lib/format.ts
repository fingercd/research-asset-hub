import type { Annotation, Asset, Job, Representation } from "../api/types";

export function truncateText(value: string, maxLength: number): string {
  const characters = Array.from(value.trim());
  if (characters.length <= maxLength) return characters.join("");
  return `${characters.slice(0, maxLength).join("")}…`;
}

export function annotationSnippet(asset: Asset): string {
  const first = asset.annotations?.find(
    (annotation) =>
      annotation.summary || annotation.body_markdown || annotation.body || annotation.title,
  );
  const value =
    asset.annotation_summary ??
    first?.summary ??
    first?.body_markdown ??
    first?.body ??
    first?.title;
  return value ? truncateText(value.replace(/\s+/g, " "), 60) : "无";
}

export function annotationBody(annotation: Annotation): string {
  return annotation.body_markdown ?? annotation.body ?? annotation.summary ?? "";
}

export function formatDate(value?: string | null, withTime = false): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "short",
    day: "numeric",
    ...(withTime
      ? ({ hour: "2-digit", minute: "2-digit" } as const)
      : {}),
  }).format(date);
}

export function formatAuthors(authors?: string[]): string {
  if (!authors?.length) return "作者未知";
  if (authors.length <= 3) return authors.join("、");
  return `${authors.slice(0, 3).join("、")} 等`;
}

export function formatBytes(bytes?: number | null): string {
  if (bytes === undefined || bytes === null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

export function representationLabel(representation: Representation): string {
  const type = representation.representation_type ?? representation.type ?? "";
  const labels: Record<string, string> = {
    pdf: "PDF",
    repository: "代码仓库",
    source_code: "源代码",
    guide: "导读",
    markdown: "Markdown",
    html: "原始 HTML",
    html_document: "HTML",
    notebook: "Notebook",
    dataset: "数据集",
    analysis: "分析",
  };
  return representation.label ?? labels[type] ?? (type || "附件");
}

export function jobError(job: Job): string | null {
  if (!job.error) return null;
  if (typeof job.error === "string") return job.error;
  return job.error.detail ?? job.error.message ?? "任务执行失败";
}

export function safeJson(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function getOpenUrl(value: unknown): string | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  for (const key of ["url", "open_url", "action_url", "href"]) {
    if (typeof record[key] === "string") return record[key];
  }
  if (Array.isArray(record.actions)) {
    for (const action of record.actions) {
      const url = getOpenUrl(action);
      if (url) return url;
    }
  }
  return null;
}
