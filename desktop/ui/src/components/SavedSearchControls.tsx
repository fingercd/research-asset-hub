import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { SavedSearch } from "../api/types";
import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { api, getApiErrorMessage } from "../api/client";
import {
  normalizeSearchDefinition,
  searchDefinitionToUrl,
} from "../lib/savedSearch";
export function SavedSearchControls({
  definition,
}: {
  definition: Record<string, unknown>;
}) {
  const client = useQueryClient();
  const location = useLocation();
  const [open, setOpen] = useState(location.hash === "#saved-searches");
  useEffect(() => { if (location.hash === "#saved-searches") setOpen(true); }, [location.hash, location.key]);
  const [name, setName] = useState("");
  const [editing, setEditing] = useState<SavedSearch | null>(null);
  const searches = useQuery({
    queryKey: ["saved-searches"],
    queryFn: ({ signal }) => api.savedSearches(signal),
    staleTime: 60_000,
  });
  const save = useMutation({
    mutationFn: () =>
      editing
        ? api.updateSavedSearch(
            editing.id,
            {
              name: name.trim(),
              definition: normalizeSearchDefinition(definition),
            },
            editing.row_version,
          )
        : api.createSavedSearch({
            name: name.trim(),
            definition: normalizeSearchDefinition(definition),
          }),
    onSuccess: () => {
      setName("");
      setEditing(null);
      void client.invalidateQueries({ queryKey: ["saved-searches"] });
    },
  });
  const remove = useMutation({
    mutationFn: (item: SavedSearch) =>
      api.deleteSavedSearch(item.id, item.row_version),
    onSuccess: () =>
      void client.invalidateQueries({ queryKey: ["saved-searches"] }),
  });
  return (
    <details className="saved-search-controls" id="saved-searches" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>保存与管理视图</summary>
      <p className="saved-view-hint">视图保存筛选条件，不复制资料；删除视图不会删除论文。侧栏可随时打开。</p>
      <div className="form-row">
        <label className="grow">
          <span className="sr-only">筛选名称</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="为当前筛选命名"
          />
        </label>
        <button
          className="button secondary compact"
          type="button"
          disabled={!name.trim() || save.isPending}
          onClick={() => save.mutate()}
        >
          {editing ? "用当前条件更新筛选" : "保存当前筛选"}
        </button>
      </div>
      {editing && (
        <button
          className="button ghost compact"
          type="button"
          onClick={() => {
            setEditing(null);
            setName("");
          }}
        >
          取消更新
        </button>
      )}
      {(save.isError || remove.isError) && (
        <p className="inline-error" role="alert">
          {getApiErrorMessage(save.error ?? remove.error)}
        </p>
      )}
      {searches.isError && <p className="inline-error" role="alert">
        保存视图加载失败：{getApiErrorMessage(searches.error)}
        <button className="button ghost compact" type="button" onClick={() => void searches.refetch()}>重试加载视图</button>
      </p>}
      {searches.isPending && <p role="status">正在加载保存视图…</p>}
      <div className="saved-search-list">
        {searches.data?.items.map((item) => (
          <div key={item.id}>
            <Link to={searchDefinitionToUrl(item.definition)}>
              {item.name ?? item.title}
            </Link>
            <button
              className="button ghost compact"
              type="button"
              onClick={() => {
                setEditing(item);
                setName(item.name ?? item.title ?? "");
              }}
            >
              更新为当前条件
            </button>
            <button
              className="button ghost compact"
              type="button"
              disabled={remove.isPending}
              onClick={() => remove.mutate(item)}
              aria-label={`删除筛选 ${item.name ?? item.title}`}
            >
              删除
            </button>
          </div>
        ))}
      </div>
    </details>
  );
}
