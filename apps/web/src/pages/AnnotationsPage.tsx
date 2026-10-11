import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { Check, Pencil, Plus, RefreshCw, Search, Trash2, X } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, getApiErrorMessage } from "../api/client";
import type { Annotation } from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";
import { annotationBody, formatDate } from "../lib/format";
import { invalidateResearchData } from "../lib/invalidateResearchData";

function EditableAnnotation({
  annotation,
  refresh,
  refreshing,
}: {
  annotation: Annotation;
  refresh: () => Promise<unknown>;
  refreshing: boolean;
}) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(annotation.title ?? "");
  const [body, setBody] = useState(annotationBody(annotation));
  const update = useMutation({
    mutationFn: () =>
      api.updateAnnotation(
        annotation.id,
        {
          title: title.trim() || null,
          body_markdown: body.trim(),
        },
        annotation.row_version,
      ),
    onSuccess: () => {
      setEditing(false);
      invalidateResearchData(queryClient, {
        annotations: true,
        assetId: annotation.asset_id,
      });
    },
  });
  const remove = useMutation({
    mutationFn: () =>
      api.deleteAnnotation(annotation.id, annotation.row_version),
    onSuccess: () =>
      invalidateResearchData(queryClient, {
        annotations: true,
        assetId: annotation.asset_id,
      }),
  });
  const removeVersionConflict =
    remove.error instanceof ApiError &&
    [412, 428].includes(remove.error.status);

  if (editing) {
    return (
      <article className="annotation-card is-editing">
        <label>
          <span className="sr-only">批注标题</span>
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
          />
        </label>
        <label>
          <span className="sr-only">批注内容</span>
          <textarea
            rows={5}
            value={body}
            onChange={(event) => setBody(event.target.value)}
          />
        </label>
        <div className="annotation-actions">
          {update.isError && (
            <span className="inline-error">
              {getApiErrorMessage(update.error)}
            </span>
          )}
          <button
            className="button primary compact"
            type="button"
            onClick={() => update.mutate()}
          >
            <Check size={14} aria-hidden="true" /> 保存
          </button>
          <button
            className="button ghost compact"
            type="button"
            onClick={() => setEditing(false)}
          >
            <X size={14} aria-hidden="true" /> 取消
          </button>
        </div>
      </article>
    );
  }

  return (
    <article className="annotation-card">
      <div>
        <span className="annotation-kind">{annotation.kind ?? "comment"}</span>
        <time>
          {formatDate(annotation.updated_at ?? annotation.created_at, true)}
        </time>
      </div>
      {annotation.title && <h3>{annotation.title}</h3>}
      {annotation.anchor?.quote &&
        annotation.anchor.quote !== annotationBody(annotation) && (
          <blockquote>{annotation.anchor.quote}</blockquote>
        )}
      <p>{annotationBody(annotation) || "空批注"}</p>
      <footer>
        <Link
          to={`/assets/${encodeURIComponent(annotation.asset_id)}${annotation.anchor?.page ? `?view=pdf&page=${annotation.anchor.page}${annotation.representation_id ? `&representation=${encodeURIComponent(annotation.representation_id)}` : ""}` : ""}`}
        >
          {annotation.anchor?.page
            ? `回到 PDF 第 ${annotation.anchor.page} 页`
            : "查看资产"}
        </Link>
        <div>
          <button
            className="icon-button"
            type="button"
            onClick={() => setEditing(true)}
          >
            <Pencil size={15} aria-hidden="true" />
            <span className="sr-only">编辑批注</span>
          </button>
          <button
            className="icon-button danger"
            type="button"
            disabled={remove.isPending}
            onClick={() => {
              if (window.confirm("确定删除这条批注？")) remove.mutate();
            }}
          >
            <Trash2 size={15} aria-hidden="true" />
            <span className="sr-only">删除批注</span>
          </button>
        </div>
      </footer>
      {remove.isError && (
        <div role="alert" className="inline-error">
          删除失败：{getApiErrorMessage(remove.error)}
          {removeVersionConflict && (
            <p>这条批注的版本已变化，请刷新并核对最新内容后再次删除。</p>
          )}
          <button
            className="button secondary compact"
            type="button"
            disabled={remove.isPending || refreshing}
            onClick={() => {
              if (removeVersionConflict) {
                void refresh()
                  .then(() => remove.reset())
                  .catch(() => undefined);
              } else {
                remove.mutate();
              }
            }}
          >
            {removeVersionConflict ? "刷新最新批注" : "重试删除"}
          </button>
        </div>
      )}
    </article>
  );
}

export function AnnotationsPage() {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [assetId, setAssetId] = useState("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const annotations = useInfiniteQuery({
    queryKey: ["annotations", "list"],
    queryFn: ({ signal, pageParam }) =>
      api.annotations(undefined, signal, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const create = useMutation({
    mutationFn: () =>
      api.createAnnotation({
        asset_id: assetId.trim(),
        kind: "comment",
        title: title.trim() || null,
        body_markdown: body.trim(),
      }),
    onSuccess: () => {
      setShowCreate(false);
      setAssetId("");
      setTitle("");
      setBody("");
      invalidateResearchData(queryClient, {
        annotations: true,
        assetId: assetId.trim(),
      });
    },
  });

  const visible = useMemo(() => {
    const items = annotations.data?.pages.flatMap((page) => page.items) ?? [];
    const value = filter.trim().toLocaleLowerCase();
    if (!value) return items;
    return items.filter((annotation) =>
      [
        annotation.title,
        annotation.summary,
        annotationBody(annotation),
        annotation.anchor?.quote,
        annotation.asset_id,
      ]
        .filter(Boolean)
        .some((item) => item?.toLocaleLowerCase().includes(value)),
    );
  }, [annotations.data?.pages, filter]);
  const loadedCount =
    annotations.data?.pages.reduce((count, page) => count + page.items.length, 0) ?? 0;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (assetId.trim() && body.trim()) create.mutate();
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="个人知识"
        title="全部批注"
        description="集中查看和维护论文总结、问题、摘录与评论；可筛选已加载的批注。"
        actions={
          <>
            <button
              className="button secondary"
              type="button"
              disabled={annotations.isFetching}
              onClick={() => void annotations.refetch()}
            >
              <RefreshCw size={16} aria-hidden="true" /> 刷新
            </button>
            <button
              className="button primary"
              type="button"
              onClick={() => setShowCreate(true)}
            >
              <Plus size={16} aria-hidden="true" /> 新建批注
            </button>
          </>
        }
      />

      {showCreate && (
        <form className="section-block annotation-create" onSubmit={submit}>
          <div className="section-heading">
            <h2>新建批注</h2>
            <button
              className="icon-button"
              type="button"
              onClick={() => setShowCreate(false)}
            >
              <X size={17} aria-hidden="true" />
              <span className="sr-only">关闭</span>
            </button>
          </div>
          <div className="form-row">
            <label className="grow">
              <span>资产 ID</span>
              <input
                required
                value={assetId}
                onChange={(event) => setAssetId(event.target.value)}
              />
            </label>
            <label className="grow">
              <span>标题（可选）</span>
              <input
                value={title}
                onChange={(event) => setTitle(event.target.value)}
              />
            </label>
          </div>
          <label>
            <span>内容</span>
            <textarea
              required
              rows={5}
              value={body}
              onChange={(event) => setBody(event.target.value)}
            />
          </label>
          <div className="form-actions">
            {create.isError && (
              <span className="inline-error">
                {getApiErrorMessage(create.error)}
              </span>
            )}
            <button
              className="button primary"
              type="submit"
              disabled={create.isPending}
            >
              {create.isPending ? "保存中…" : "保存批注"}
            </button>
          </div>
        </form>
      )}

      <label className="list-search">
        <Search size={16} aria-hidden="true" />
        <span className="sr-only">筛选已加载批注</span>
        <input
          type="search"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="筛选已加载的批注内容或资产 ID…"
        />
      </label>
      {annotations.data && (
        <p className="muted">
          已加载 {loadedCount} 条批注。筛选仅作用于已加载内容。
          {annotations.hasNextPage && "加载更多可扩大筛选范围。"}
        </p>
      )}

      {annotations.isPending ? (
        <LoadingState label="正在加载批注…" />
      ) : annotations.isError && !annotations.data ? (
        <ErrorState
          error={annotations.error}
          retry={() => void annotations.refetch()}
        />
      ) : visible.length ? (
        <section className="annotation-grid" aria-label="批注列表">
          {visible.map((annotation) => (
            <EditableAnnotation
              annotation={annotation}
              key={annotation.id}
              refresh={() => annotations.refetch({ throwOnError: true })}
              refreshing={annotations.isFetching}
            />
          ))}
        </section>
      ) : (
        <EmptyState
          title={filter ? "已加载批注中没有匹配内容" : "还没有批注"}
          detail={
            filter ? "尝试其他筛选词，或加载更多批注。" : "打开任一资产详情即可记录第一条批注。"
          }
          kind={filter ? "search" : "empty"}
        />
      )}
      {annotations.isError && annotations.data && (
        <ErrorState
          error={annotations.error}
          retry={() =>
            void (annotations.isFetchNextPageError
              ? annotations.fetchNextPage()
              : annotations.refetch())
          }
        />
      )}
      {annotations.hasNextPage && (
        <div className="load-more">
          <button
            className="button secondary"
            type="button"
            disabled={annotations.isFetching}
            onClick={() => void annotations.fetchNextPage()}
          >
            {annotations.isFetchingNextPage ? "正在加载…" : "加载更多批注"}
          </button>
        </div>
      )}
    </div>
  );
}
