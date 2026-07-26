import { useMutation } from "@tanstack/react-query";
import {
  BookOpenText,
  ChevronDown,
  Code2,
  ExternalLink,
  FileText,
} from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type {
  QuickAction,
  QuickActionAlternative,
  QuickActionKind,
  QuickActionStatus,
} from "../api/types";
import { getOpenUrl } from "../lib/format";

const actionDefinitions: Array<{
  kind: QuickActionKind;
  label: string;
  icon: typeof FileText;
}> = [
  { kind: "pdf", label: "PDF", icon: FileText },
  { kind: "html", label: "HTML", icon: ExternalLink },
  { kind: "guide", label: "导读", icon: BookOpenText },
  { kind: "code", label: "代码", icon: Code2 },
];

const statusReasons: Record<Exclude<QuickActionStatus, "available">, string> = {
  missing: "文件已登记，但当前内容缺失。",
  not_provided: "该研究单元未提供此入口。",
  network_required: "需要联网后才能打开。",
};

function unavailableLabel(kind: QuickActionKind, label: string, status: QuickActionStatus) {
  if (kind === "pdf" && (status === "missing" || status === "not_provided")) {
    return "PDF缺失";
  }
  return label;
}

function actionTitle(action: QuickAction, label: string): string {
  const alternatives = action.alternatives?.length ?? 0;
  if (action.status === "missing" || action.status === "not_provided") {
    const reason = action.reason || statusReasons[action.status];
    return `${label}：${reason}${alternatives ? ` 另有 ${alternatives} 个可用入口。` : ""}`;
  }
  const relation = action.relation_type ? ` · ${action.relation_type}` : "";
  const more = alternatives ? ` · 另有 ${alternatives} 个入口` : "";
  const network =
    action.status === "network_required"
      ? ` · ${action.reason || statusReasons.network_required}`
      : "";
  return `打开${label}${relation}${network}${more}`;
}

interface ActionChoice {
  key: string;
  representationId: string;
  label: string;
  role?: string | null;
  relationType?: string | null;
  status: QuickActionStatus;
  isDefault: boolean;
}

function alternativeLabel(
  alternative: QuickActionAlternative,
  index: number,
): string {
  return (
    alternative.label?.trim() ||
    alternative.role?.trim() ||
    `备用入口 ${index + 1}`
  );
}

function actionChoices(action: QuickAction, label: string): ActionChoice[] {
  const choices: ActionChoice[] = [];
  if (action.representation_id) {
    choices.push({
      key: `default:${action.representation_id}`,
      representationId: action.representation_id,
      label: `${label} 默认入口`,
      relationType: action.relation_type,
      status: action.status,
      isDefault: true,
    });
  }
  action.alternatives?.forEach((alternative, index) => {
    if (!alternative.representation_id) return;
    choices.push({
      key: `alternative:${alternative.representation_id}`,
      representationId: alternative.representation_id,
      label: alternativeLabel(alternative, index),
      role: alternative.role,
      relationType: alternative.relation_type,
      status: alternative.status,
      isDefault: false,
    });
  });
  return choices;
}

function choiceMeta(choice: ActionChoice): string {
  const values = [
    choice.isDefault ? "默认" : choice.role,
    choice.relationType,
    choice.status === "network_required" ? "需联网" : null,
  ].filter((value): value is string => Boolean(value));
  return values.join(" · ");
}

export function ResearchUnitActions({
  actions,
  ownerAssetId,
  repositoryOnly = false,
}: {
  actions: QuickAction[];
  ownerAssetId: string;
  repositoryOnly?: boolean;
}) {
  const mutation = useMutation({
    mutationFn: (representationId: string) =>
      api.openRepresentation(representationId),
    onSuccess: (payload) => {
      const url = getOpenUrl(payload);
      if (url) window.open(url, "_blank", "noopener,noreferrer");
    },
  });

  const visibleDefinitions = repositoryOnly
    ? actionDefinitions.filter((definition) => definition.kind === "code")
    : actionDefinitions;

  return (
    <div className="quick-actions" aria-label="快速打开">
      {visibleDefinitions.map((definition) => {
        const action = actions.find((item) => item.kind === definition.kind) ?? {
          kind: definition.kind,
          status: "not_provided" as const,
          owner_asset_id: ownerAssetId,
          alternatives: [],
        };
        const Icon = definition.icon;
        const label = unavailableLabel(
          definition.kind,
          definition.label,
          action.status,
        );
        const title = actionTitle(action, definition.label);
        const alternative = action.alternatives?.find(
          (item) =>
            ["available", "network_required"].includes(item.status) &&
            item.representation_id,
        );
        const representationId =
          action.representation_id ?? alternative?.representation_id;
        const actionOwnerId =
          action.owner_asset_id || alternative?.owner_asset_id || ownerAssetId;
        const choices = actionChoices(action, definition.label);
        const alternativeCount = Math.max(0, choices.length - 1);
        const requiresNetwork = action.status === "network_required";
        const accessibleLabel = requiresNetwork
          ? `${definition.label}（需联网）`
          : definition.label;
        const content = (
          <>
            <Icon size={14} aria-hidden="true" />
            {label}
            {alternativeCount > 0 && (
              <span className="quick-action-count" aria-hidden="true">
                +{alternativeCount}
              </span>
            )}
            {requiresNetwork && (
              <span className="quick-action-network">需联网</span>
            )}
          </>
        );

        if (action.status === "missing" || action.status === "not_provided") {
          return (
            <span
              className="quick-action-tooltip"
              title={title}
              key={definition.kind}
            >
              <button
                className="quick-action is-unavailable"
                type="button"
                disabled
                title={title}
                aria-label={label}
              >
                {content}
              </button>
            </span>
          );
        }

        if (!representationId) {
          return (
            <Link
              className="quick-action"
              to={`/assets/${encodeURIComponent(actionOwnerId)}`}
              title={title}
              aria-label={definition.label}
              key={definition.kind}
            >
              {content}
            </Link>
          );
        }

        if (choices.length > 1) {
          return (
            <div className="quick-action-split" key={definition.kind}>
              <button
                className={`quick-action ${
                  requiresNetwork ? "requires-network" : ""
                }`}
                type="button"
                disabled={mutation.isPending}
                title={title}
                aria-label={accessibleLabel}
                onClick={() => mutation.mutate(representationId)}
              >
                {content}
              </button>
              <details
                className="quick-action-choice"
                onKeyDown={(event) => {
                  if (event.key !== "Escape") return;
                  event.currentTarget.open = false;
                  const summary = event.currentTarget.querySelector("summary");
                  if (summary instanceof HTMLElement) summary.focus();
                }}
              >
                <summary
                  aria-label={`选择${definition.label}入口，共 ${choices.length} 个`}
                  title={`选择${definition.label}入口`}
                >
                  <ChevronDown size={13} aria-hidden="true" />
                </summary>
                <div
                  className="quick-action-choice-menu"
                  role="group"
                  aria-label={`${definition.label}入口`}
                >
                  {choices.map((choice) => {
                    const meta = choiceMeta(choice);
                    const choiceRequiresNetwork =
                      choice.status === "network_required";
                    return (
                      <button
                        type="button"
                        className="quick-action-choice-item"
                        disabled={mutation.isPending}
                        aria-label={`${choice.label}${
                          choiceRequiresNetwork ? "（需联网）" : ""
                        }`}
                        onClick={(event) => {
                          const details =
                            event.currentTarget.closest("details");
                          const summary = details?.querySelector("summary");
                          details?.removeAttribute("open");
                          if (summary instanceof HTMLElement) summary.focus();
                          mutation.mutate(choice.representationId);
                        }}
                        key={choice.key}
                      >
                        <span>
                          <strong>{choice.label}</strong>
                          {meta && <small>{meta}</small>}
                        </span>
                        {choiceRequiresNetwork && (
                          <span className="quick-action-network">需联网</span>
                        )}
                      </button>
                    );
                  })}
                </div>
              </details>
            </div>
          );
        }

        return (
          <button
            className={`quick-action ${
              requiresNetwork ? "requires-network" : ""
            }`}
            type="button"
            disabled={mutation.isPending}
            title={title}
            aria-label={accessibleLabel}
            onClick={() => mutation.mutate(representationId)}
            key={definition.kind}
          >
            {content}
          </button>
        );
      })}
      {mutation.isError && <span className="inline-error">无法打开</span>}
    </div>
  );
}
