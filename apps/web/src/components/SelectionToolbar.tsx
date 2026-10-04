import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, getApiErrorMessage } from "../api/client";
import type { ReadingStatus } from "../api/types";
import { workflowItemError } from "../lib/workflowResult";
import { PackageActions } from "./PackageActions";

export function SelectionToolbar({
  ids,
  clear,
}: {
  ids: string[];
  clear: () => void;
}) {
  const client = useQueryClient();
  const [status, setStatus] = useState<ReadingStatus | "">("");
  const [tags, setTags] = useState("");
  const [tagAction, setTagAction] = useState("add");
  const [domain, setDomain] = useState("");
  const [domainAction, setDomainAction] = useState("add");
  const [collection, setCollection] = useState("");
  const [collectionAction, setCollectionAction] = useState("add");
  const collections = useQuery({
    queryKey: ["collections"],
    queryFn: ({ signal }) => api.collections(signal),
  });
  const domains = useQuery({
    queryKey: ["domains"],
    queryFn: ({ signal }) => api.domains(signal),
  });
  const update = useMutation({
    mutationFn: () => {
      const tagList = tags
        .split(/[,，]/)
        .map((v) => v.trim())
        .filter(Boolean);
      return api.bulkAssets({
        asset_ids: ids,
        ...(status ? { reading_status: status } : {}),
        ...(tagList.length
          ? { [tagAction === "add" ? "add_tags" : "remove_tags"]: tagList }
          : {}),
        ...(domain
          ? {
              [domainAction === "add" ? "add_domains" : "remove_domains"]: [
                domain,
              ],
            }
          : {}),
        ...(collection
          ? { collection_id: collection, collection_action: collectionAction }
          : {}),
      });
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["search"] });
      void client.invalidateQueries({ queryKey: ["collections"] });
      void client.invalidateQueries({ queryKey: ["collection-members"] });
      void client.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
  if (!ids.length) return null;
  return (
    <section className="selection-toolbar" aria-label="批量操作">
      <div className="section-heading">
        <strong>已选择 {ids.length} 项</strong>
        <button className="button ghost compact" type="button" onClick={clear}>
          取消选择
        </button>
      </div>
      <div className="advanced-filter-grid">
        <label>
          <span>阅读状态</span>
          <select
            aria-label="阅读状态"
            value={status}
            onChange={(e) => setStatus(e.target.value as ReadingStatus | "")}
          >
            <option value="">不修改</option>
            <option value="unread">未读</option>
            <option value="reading">阅读中</option>
            <option value="read">已读</option>
          </select>
        </label>
        <label>
          <span>标签</span>
          <input
            aria-label="批量标签"
            value={tags}
            onChange={(e) => setTags(e.target.value)}
            placeholder="逗号分隔"
          />
          <select
            aria-label="标签操作"
            value={tagAction}
            onChange={(e) => setTagAction(e.target.value)}
          >
            <option value="add">添加标签</option>
            <option value="remove">移除标签</option>
          </select>
        </label>
        <label>
          <span>领域</span>
          <select
            aria-label="批量领域"
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
          >
            <option value="">不修改</option>
            {domains.data?.items.map((d) => (
              <option key={d.slug} value={d.slug}>
                {d.label ?? d.slug}
              </option>
            ))}
          </select>
          <select
            aria-label="领域操作"
            value={domainAction}
            onChange={(e) => setDomainAction(e.target.value)}
          >
            <option value="add">添加领域</option>
            <option value="remove">移除领域</option>
          </select>
        </label>
        <label>
          <span>收藏集合</span>
          <select
            aria-label="批量收藏集合"
            value={collection}
            onChange={(e) => setCollection(e.target.value)}
          >
            <option value="">不修改</option>
            {collections.data?.items.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label ?? c.title ?? c.name}
              </option>
            ))}
          </select>
          <select
            aria-label="集合操作"
            value={collectionAction}
            onChange={(e) => setCollectionAction(e.target.value)}
          >
            <option value="add">加入集合</option>
            <option value="remove">移出集合</option>
          </select>
        </label>
      </div>
      <div className="button-row">
        <button
          className="button primary compact"
          type="button"
          disabled={
            update.isPending || !(status || tags.trim() || domain || collection)
          }
          onClick={() => update.mutate()}
        >
          {update.isPending ? "正在应用…" : "应用批量修改"}
        </button>
        <PackageActions assetIds={ids} />
      </div>
      {update.isError && (
        <p className="inline-error" role="alert">
          {getApiErrorMessage(update.error)}
        </p>
      )}
      {update.data && (
        <div role="status">
          {update.data.items.filter((v) => v.status !== "failed").length}{" "}
          项已处理。
          {update.data.items
            .filter((v) => v.status === "failed")
            .map((v) => (
              <p className="inline-error" key={v.id}>
                {v.id}：{workflowItemError(v)}
              </p>
            ))}
        </div>
      )}
    </section>
  );
}
