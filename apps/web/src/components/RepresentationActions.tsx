import { useMutation } from "@tanstack/react-query";
import { BookOpenText, Code2, ExternalLink, FileText } from "lucide-react";
import { api } from "../api/client";
import { getRepresentationType, type Representation } from "../api/types";
import { getOpenUrl } from "../lib/format";

interface QuickAction {
  key: "pdf" | "code" | "guide" | "html";
  label: string;
  icon: typeof FileText;
  match: (representation: Representation) => boolean;
}

const actions: QuickAction[] = [
  {
    key: "pdf",
    label: "PDF",
    icon: FileText,
    match: (item) => getRepresentationType(item) === "pdf",
  },
  {
    key: "html",
    label: "HTML",
    icon: ExternalLink,
    match: (item) =>
      ["html", "html_document"].includes(getRepresentationType(item)) &&
      item.role !== "guide",
  },
  {
    key: "guide",
    label: "导读",
    icon: BookOpenText,
    match: (item) =>
      ["guide", "analysis", "markdown"].includes(getRepresentationType(item)) ||
      item.role === "guide",
  },
  {
    key: "code",
    label: "代码",
    icon: Code2,
    match: (item) =>
      ["repository", "source_code", "notebook"].includes(getRepresentationType(item)),
  },
];

function directUrl(representation?: Representation): string | null {
  return (
    representation?.open_url ??
    representation?.external_url ??
    representation?.url ??
    null
  );
}

export function RepresentationActions({
  representations = [],
  showUnavailable = true,
}: {
  representations?: Representation[];
  showUnavailable?: boolean;
}) {
  const mutation = useMutation({
    mutationFn: (representation: Representation) =>
      api.openRepresentation(representation.id),
    onSuccess: (payload) => {
      const url = getOpenUrl(payload);
      if (url) window.open(url, "_blank", "noopener,noreferrer");
    },
  });

  return (
    <div className="quick-actions" aria-label="快速打开">
      {actions.map((action) => {
        const representation = representations.find(
          (item) =>
            action.match(item) &&
            (item.availability === undefined || item.availability === "available"),
        );
        if (!representation && !showUnavailable) return null;
        const url = directUrl(representation);
        const Icon = action.icon;

        if (url) {
          return (
            <a
              className="quick-action"
              href={url}
              target="_blank"
              rel="noreferrer"
              key={action.key}
            >
              <Icon size={14} aria-hidden="true" />
              {action.label}
            </a>
          );
        }

        return (
          <button
            className="quick-action"
            type="button"
            disabled={!representation || mutation.isPending}
            title={representation ? `打开${action.label}` : `暂无${action.label}`}
            onClick={() => representation && mutation.mutate(representation)}
            key={action.key}
          >
            <Icon size={14} aria-hidden="true" />
            {action.label}
          </button>
        );
      })}
      {mutation.isError && <span className="inline-error">无法打开</span>}
    </div>
  );
}
