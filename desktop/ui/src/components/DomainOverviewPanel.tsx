import { useQuery } from "@tanstack/react-query";
import { ExternalLink, MonitorUp, Palette, RotateCcw } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type {
  DomainOverview,
  RepresentationPresentation,
} from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "./PageState";

function canOpenOverview(overview?: DomainOverview | null): boolean {
  return Boolean(
    overview?.representation_id &&
      ["available", "network_required"].includes(overview.status),
  );
}

export function DomainOverviewPanel({
  overview,
  domainName,
  fallback,
}: {
  overview?: DomainOverview | null;
  domainName: string;
  fallback: ReactNode;
}) {
  const [presentation, setPresentation] =
    useState<RepresentationPresentation>(
      overview?.default_presentation ?? "hub",
    );
  const representationId = overview?.representation_id ?? null;
  const canOpen = canOpenOverview(overview);

  useEffect(() => {
    setPresentation(overview?.default_presentation ?? "hub");
  }, [overview?.default_presentation, representationId]);

  const action = useQuery({
    queryKey: [
      "domain-overview-open-action",
      representationId,
      presentation,
    ],
    queryFn: () =>
      api.openRepresentation(representationId!, {
        presentation,
        embed: true,
      }),
    enabled: canOpen,
    staleTime: 4 * 60_000,
  });

  if (!canOpen) {
    return (
      <div className="domain-overview-fallback">
        <EmptyState
          title="领域总览暂不可用"
          detail={
            overview?.warning ??
            "尚未登记可打开的领域整体 HTML，先显示原生研究单元目录。"
          }
          kind="index"
        />
        {fallback}
      </div>
    );
  }

  const url = action.data?.url;
  const appliedPresentation = action.data?.presentation ?? presentation;
  return (
    <section
      className="domain-overview-panel"
      aria-labelledby="domain-overview-title"
    >
      <div className="domain-overview-toolbar">
        <div>
          <p className="eyebrow">领域整体 HTML</p>
          <h2 id="domain-overview-title">{domainName} · 领域总览</h2>
          <p className="muted">
            源文件保持只读；统一样式仅在隔离网关响应中临时应用。
          </p>
        </div>
        <div className="domain-overview-controls">
          <div
            className="presentation-switch"
            role="group"
            aria-label="领域总览样式"
          >
            <button
              className={presentation === "hub" ? "is-active" : undefined}
              type="button"
              aria-pressed={presentation === "hub"}
              onClick={() => setPresentation("hub")}
            >
              <Palette size={14} aria-hidden="true" />
              统一样式
            </button>
            <button
              className={presentation === "original" ? "is-active" : undefined}
              type="button"
              aria-pressed={presentation === "original"}
              onClick={() => setPresentation("original")}
            >
              <RotateCcw size={14} aria-hidden="true" />
              原始样式
            </button>
          </div>
          {url && (
            <a
              className="button secondary"
              href={url}
              target="_blank"
              rel="noreferrer"
            >
              <ExternalLink size={14} aria-hidden="true" />
              新窗口打开
            </a>
          )}
        </div>
      </div>

      {overview?.warning && (
        <p className="domain-overview-warning" role="status">
          {overview.warning}
        </p>
      )}
      {action.data?.warning && (
        <p className="domain-overview-warning" role="status">
          {action.data.warning}
        </p>
      )}
      {action.isPending ? (
        <LoadingState label="正在建立领域总览会话…" />
      ) : action.isError ? (
        <div className="domain-overview-fallback">
          <ErrorState
            error={action.error}
            retry={() => void action.refetch()}
          />
          {fallback}
        </div>
      ) : url ? (
        <>
          <div className="domain-overview-frame-shell">
            <div className="domain-overview-frame-status">
              <MonitorUp size={14} aria-hidden="true" />
              当前展示：
              {appliedPresentation === "hub" ? "Hub 统一样式" : "来源原始样式"}
            </div>
            <iframe
              key={url}
              className="domain-overview-frame"
              src={url}
              title={`${domainName}领域总览`}
              sandbox="allow-downloads allow-forms allow-popups allow-same-origin allow-scripts"
              referrerPolicy="no-referrer"
            />
          </div>
        </>
      ) : (
        <div className="domain-overview-fallback">
          <EmptyState
            title="未能取得领域总览地址"
            detail="隔离网关没有返回可打开地址，先显示原生研究单元目录。"
          />
          {fallback}
        </div>
      )}
    </section>
  );
}
