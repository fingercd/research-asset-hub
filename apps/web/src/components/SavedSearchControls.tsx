import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { SavedSearch } from "../api/types";
import { useState } from "react";
import { Link } from "react-router-dom";
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
  const [name, setName] = useState("");
  const [editing, setEditing] = useState<SavedSearch | null>(null);
  const searches = useQuery({
    queryKey: ["saved-searches"],
    queryFn: ({ signal }) => api.savedSearches(signal),
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
    <details className="saved-search-controls">
      <summary>保存与复用筛选</summary>
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
