import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";
import { ArrowRight, Check, GitMerge, RefreshCw, ShieldAlert } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, getApiErrorMessage } from "../api/client";
import type { ApiList, SyncConflict } from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";
import { formatDate, safeJson } from "../lib/format";
import { invalidateResearchData } from "../lib/invalidateResearchData";

// Keep this list aligned with the Obsidian sync contract's editable fields.
const mergeFields: Record<
  string,
  { label: string; kind: "text" | "list" | "number" | "reading" | "visibility" }
> = {
  title: { label: "题名", kind: "text" },
  authors: { label: "作者", kind: "list" },
  year: { label: "年份", kind: "number" },
  venue: { label: "发表场所", kind: "text" },
  abstract: { label: "摘要", kind: "text" },
  summary: { label: "简介", kind: "text" },
  domains: { label: "领域标识", kind: "list" },
  tags: { label: "标签", kind: "list" },
  reading_status: { label: "阅读状态", kind: "reading" },
  visibility: { label: "可见范围", kind: "visibility" },
};

function ConflictCard({ conflict }: { conflict: SyncConflict }) {
  const queryClient = useQueryClient();
  const field = (conflict.field ?? "").replace(/^\//, "");
  const mergeField = mergeFields[field];
  const [merging, setMerging] = useState(false);
  const [mergeInput, setMergeInput] = useState("");
  const [inputError, setInputError] = useState("");
  const resolve = useMutation({
    mutationFn: (choice: {
      strategy: "keep_local" | "accept_incoming" | "merge";
      value?: unknown;
    }) =>
      api.resolveConflict(conflict.id, choice.strategy, conflict.row_version, choice.value),
    onSuccess: () => {
      queryClient.setQueryData<InfiniteData<ApiList<SyncConflict>>>(
        ["conflicts", "pending"],
        (data) =>
          data && {
            ...data,
            pages: data.pages.map((page) => ({
              ...page,
              items: page.items.filter((item) => item.id !== conflict.id),
            })),
          },
      );
      void queryClient.invalidateQueries({ queryKey: ["conflicts"] });
      invalidateResearchData(queryClient, {
        assetId: conflict.asset_id ?? undefined,
        annotations: true,
      });
    },
  });
  const submitMerge = (event: FormEvent) => {
    event.preventDefault();
    if (field === "title" && !mergeInput.trim()) {
      setInputError("题名不能为空。");
      return;
    }
    setInputError("");
    const value =
      mergeField?.kind === "list"
        ? mergeInput.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
        : mergeField?.kind === "number"
          ? mergeInput === "" ? null : Number(mergeInput)
          : mergeInput.trim() || null;
    resolve.mutate({ strategy: "merge", value });
  };
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
        <span>确认后会将所选字段同步到 Hub 和 Obsidian 笔记，不会改写论文来源原件。</span>
        <div>
          <button
            className="button secondary compact"
            type="button"
            disabled={resolve.isPending || merging}
            onClick={() => resolve.mutate({ strategy: "keep_local" })}
          >
            <Check size={14} aria-hidden="true" /> 保留本地
          </button>
          <button
            className="button secondary compact"
            type="button"
            disabled={resolve.isPending || merging || !mergeField}
            onClick={() => resolve.mutate({ strategy: "accept_incoming" })}
          >
            <RefreshCw size={14} aria-hidden="true" /> 采用来源
          </button>
          {mergeField && (
            <button
              className="button primary compact"
              type="button"
              disabled={resolve.isPending || merging}
              onClick={() => {
                const value = conflict.local_value;
                setMergeInput(
                  Array.isArray(value)
                    ? value.join("\n")
                    : value === null || value === undefined ? "" : String(value),
                );
                setInputError("");
                resolve.reset();
                setMerging(true);
              }}
            >
              <GitMerge size={14} aria-hidden="true" /> 合并
            </button>
          )}
        </div>
      </footer>
      {!mergeField && (
        <p>这是系统管理字段，只能保留 Hub 值并修正 Obsidian 笔记。</p>
      )}
      {merging && mergeField && (
        <form onSubmit={submitMerge} className="section-block conflict-merge-form">
          <label htmlFor={`merge-value-${conflict.id}`}>合并后的{mergeField.label}</label>
            {mergeField.kind === "reading" || mergeField.kind === "visibility" ? (
              <select
                id={`merge-value-${conflict.id}`}
                value={mergeInput}
                onChange={(event) => setMergeInput(event.target.value)}
                disabled={resolve.isPending}
                required
              >
                {mergeField.kind === "reading" ? (
                  <>
                    <option value="unread">未读</option>
                    <option value="reading">在读</option>
                    <option value="read">已读</option>
                  </>
                ) : (
                  <>
                    <option value="private">私有</option>
                    <option value="internal">内部</option>
                    <option value="public">公开</option>
                  </>
                )}
              </select>
            ) : mergeField.kind === "number" ? (
              <input
                id={`merge-value-${conflict.id}`}
                type="number"
                min={1450}
                max={2200}
                step={1}
                value={mergeInput}
                onChange={(event) => setMergeInput(event.target.value)}
                disabled={resolve.isPending}
              />
            ) : (
              <textarea
                id={`merge-value-${conflict.id}`}
                rows={4}
                value={mergeInput}
                onChange={(event) => setMergeInput(event.target.value)}
                disabled={resolve.isPending}
                required={field === "title"}
              />
            )}
          {mergeField.kind === "list" && <p>每行填写一项；留空表示清空列表。</p>}
          {mergeField.kind === "number" && <p>留空表示清除年份。</p>}
          <p>核对合并值后确认，Hub 和 Obsidian 笔记将使用这个值。</p>
          {inputError && (
            <span role="alert" className="inline-error">{inputError}</span>
          )}
          <div className="form-actions">
            <button
              type="submit"
              className="button primary compact"
              disabled={resolve.isPending}
            >
              {resolve.isPending ? "同步中…" : "确认合并并同步"}
            </button>
            <button
              type="button"
              className="button ghost compact"
              disabled={resolve.isPending}
              onClick={() => {
                setMerging(false);
                resolve.reset();
              }}
            >
              取消合并
            </button>
          </div>
        </form>
      )}
      {resolve.isError && (
        <span className="inline-error" role="alert">{getApiErrorMessage(resolve.error)}</span>
      )}
    </article>
  );
}

export function ConflictsPage() {
  const conflicts = useInfiniteQuery({
    queryKey: ["conflicts", "pending"],
    queryFn: ({ signal, pageParam }) => api.conflicts(signal, pageParam, "pending"),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const items = conflicts.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <div className="page">
      <PageHeader
        eyebrow="来源同步"
        title="同步冲突"
        description="逐项审查 Obsidian 笔记变化。确认选择后会将所选字段同步到 Hub 和 Obsidian 笔记，不会改写论文来源原件。"
        actions={
          <button
            className="button secondary"
            type="button"
            disabled={conflicts.isFetching}
            onClick={() => void conflicts.refetch()}
          >
            <RefreshCw size={15} aria-hidden="true" /> 刷新
          </button>
        }
      />
      {conflicts.isPending ? (
        <LoadingState label="正在检查同步冲突…" />
      ) : conflicts.isError && !conflicts.data ? (
        <ErrorState error={conflicts.error} retry={() => void conflicts.refetch()} />
      ) : items.length ? (
        <section className="conflict-list" aria-label="待解决冲突">
          <div className="notice is-warning">
            <ShieldAlert size={17} aria-hidden="true" />
            <div>
              <strong>已加载 {items.length} 项待处理冲突</strong>
              <span>请核对本地整理结果与来源新值后再选择。</span>
            </div>
          </div>
          {items.map((conflict) => (
            <ConflictCard conflict={conflict} key={conflict.id} />
          ))}
        </section>
      ) : (
        <EmptyState
          title="没有待处理冲突"
          detail="当前本地目录与最近一次来源同步一致。"
        />
      )}
      {conflicts.isError && conflicts.data && (
        <ErrorState
          error={conflicts.error}
          retry={() =>
            void (conflicts.isFetchNextPageError
              ? conflicts.fetchNextPage()
              : conflicts.refetch())
          }
        />
      )}
      {conflicts.hasNextPage && (
        <div className="load-more">
          <button
            className="button secondary"
            type="button"
            disabled={conflicts.isFetching}
            onClick={() => void conflicts.fetchNextPage()}
          >
            {conflicts.isFetchingNextPage ? "正在加载…" : "加载更多冲突"}
          </button>
        </div>
      )}
    </div>
  );
}
