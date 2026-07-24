import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Check, GitMerge, RefreshCw, ShieldAlert } from "lucide-react";
import { Link } from "react-router-dom";
import { api, getApiErrorMessage } from "../api/client";
import type { SyncConflict } from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";
import { formatDate, safeJson } from "../lib/format";

function ConflictCard({ conflict }: { conflict: SyncConflict }) {
  const queryClient = useQueryClient();
  const resolve = useMutation({
    mutationFn: (strategy: "keep_local" | "accept_incoming" | "merge") =>
      api.resolveConflict(conflict.id, strategy, conflict.row_version),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["conflicts"] }),
  });
  return (
    <article className="conflict-card">
      <header>
        <span className="conflict-icon"><ShieldAlert size={18} aria-hidden="true" /></span>
        <div>
          <span>{conflict.entity_type ?? "资产"} · {conflict.field ?? "多字段"}</span>
          <h2>{conflict.title ?? "检测到来源差异"}</h2>
          <small>{formatDate(conflict.detected_at, true)} · {conflict.source ?? "未知来源"}</small>
        </div>
        {conflict.asset_id && (
          <Link to={`/assets/${encodeURIComponent(conflict.asset_id)}`}>
            查看资产 <ArrowRight size={13} aria-hidden="true" />
          </Link>
        )}
      </header>
      <div className="conflict-values">
        <div>
          <strong>本地值</strong>
          <pre>{safeJson(conflict.local_value)}</pre>
        </div>
        <div>
          <strong>来源新值</strong>
          <pre>{safeJson(conflict.incoming_value)}</pre>
        </div>
      </div>
      <footer>
        <span>选择后会记录解决策略，不会改写来源目录。</span>
        <div>
          <button
            className="button secondary compact"
            type="button"
            disabled={resolve.isPending}
            onClick={() => resolve.mutate("keep_local")}
          >
            <Check size={14} aria-hidden="true" /> 保留本地
          </button>
          <button
            className="button secondary compact"
            type="button"
            disabled={resolve.isPending}
            onClick={() => resolve.mutate("accept_incoming")}
          >
            <RefreshCw size={14} aria-hidden="true" /> 采用来源
          </button>
          <button
            className="button primary compact"
            type="button"
            disabled={resolve.isPending}
            onClick={() => resolve.mutate("merge")}
          >
            <GitMerge size={14} aria-hidden="true" /> 合并
          </button>
        </div>
      </footer>
      {resolve.isError && (
        <span className="inline-error" role="alert">{getApiErrorMessage(resolve.error)}</span>
      )}
    </article>
  );
}

export function ConflictsPage() {
  const conflicts = useQuery({
    queryKey: ["conflicts"],
    queryFn: ({ signal }) => api.conflicts(signal),
  });

  return (
    <div className="page">
      <PageHeader
        eyebrow="来源同步"
        title="同步冲突"
        description="逐项审查来源变化。任何解决操作只更新 Hub 状态，不会修改只读来源文件。"
      />
      {conflicts.isPending ? (
        <LoadingState label="正在检查同步冲突…" />
      ) : conflicts.isError ? (
        <ErrorState error={conflicts.error} retry={() => void conflicts.refetch()} />
      ) : conflicts.data.items.length ? (
        <section className="conflict-list" aria-label="待解决冲突">
          <div className="notice is-warning">
            <ShieldAlert size={17} aria-hidden="true" />
            <div>
              <strong>{conflicts.data.items.length} 项待处理</strong>
              <span>请核对本地整理结果与来源新值后再选择。</span>
            </div>
          </div>
          {conflicts.data.items.map((conflict) => (
            <ConflictCard conflict={conflict} key={conflict.id} />
          ))}
        </section>
      ) : (
        <EmptyState
          title="没有待处理冲突"
          detail="当前本地目录与最近一次来源同步一致。"
        />
      )}
    </div>
  );
}
