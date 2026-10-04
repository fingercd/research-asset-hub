import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil, Plus, Search, Trash2, X } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, getApiErrorMessage } from "../api/client";
import type { Annotation } from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";
import { annotationBody, formatDate } from "../lib/format";
import { invalidateResearchData } from "../lib/invalidateResearchData";

function EditableAnnotation({ annotation }: { annotation: Annotation }) {
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
  const annotations = useQuery({
    queryKey: ["annotations"],
    queryFn: ({ signal }) => api.annotations(undefined, signal),
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
    const value = filter.trim().toLocaleLowerCase();
    if (!value) return annotations.data?.items ?? [];
    return (annotations.data?.items ?? []).filter((annotation) =>
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
  }, [annotations.data?.items, filter]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (assetId.trim() && body.trim()) create.mutate();
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="个人知识"
        title="全部批注"
        description="集中查看、检索和维护论文总结、问题、摘录与评论。"
        actions={
          <button
            className="button primary"
            type="button"
            onClick={() => setShowCreate(true)}
          >
            <Plus size={16} aria-hidden="true" /> 新建批注
          </button>
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
        <span className="sr-only">筛选批注</span>
        <input
          type="search"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="筛选批注内容或资产 ID…"
        />
      </label>

      {annotations.isPending ? (
        <LoadingState label="正在加载批注…" />
      ) : annotations.isError ? (
        <ErrorState
          error={annotations.error}
          retry={() => void annotations.refetch()}
        />
      ) : visible.length ? (
        <section className="annotation-grid" aria-label="批注列表">
          {visible.map((annotation) => (
            <EditableAnnotation annotation={annotation} key={annotation.id} />
          ))}
        </section>
      ) : (
        <EmptyState
          title={filter ? "没有匹配的批注" : "还没有批注"}
          detail={
            filter ? "尝试其他筛选词。" : "打开任一资产详情即可记录第一条批注。"
          }
          kind={filter ? "search" : "empty"}
        />
      )}
    </div>
  );
}
