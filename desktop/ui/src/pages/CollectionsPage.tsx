import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useState } from "react";
import { api, getApiErrorMessage } from "../api/client";
import type { Collection } from "../api/types";
import { AssetCard } from "../components/AssetCard";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";
import { workflowItemError } from "../lib/workflowResult";
import { SelectionToolbar } from "../components/SelectionToolbar";
export function CollectionsPage() {
  const client = useQueryClient();
  const [active, setActive] = useState("");
  const [editing, setEditing] = useState<Collection | null>(null);
  const [label, setLabel] = useState("");
  const [description, setDescription] = useState("");
  const [ids, setIds] = useState<string[]>([]);
  const [newForm, setNewForm] = useState(false);
  const list = useQuery({
    queryKey: ["collections"],
    queryFn: ({ signal }) => api.collections(signal),
  });
  const members = useInfiniteQuery({
    queryKey: ["collection-members", active],
    queryFn: ({ signal, pageParam }) =>
      api.collectionAssets(active, signal, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: !!active,
  });
  const save = useMutation({
    mutationFn: () =>
      editing
        ? api.updateCollection(
            editing.id,
            { label: label.trim(), description: description.trim() },
            editing.row_version,
          )
        : api.createCollection({
            label: label.trim(),
            description: description.trim(),
          }),
    onSuccess: (value) => {
      setNewForm(false);
      setEditing(null);
      setLabel("");
      setDescription("");
      setActive(value.id);
      void client.invalidateQueries({ queryKey: ["collections"] });
    },
  });
  const remove = useMutation({
    mutationFn: (item: Collection) =>
      api.deleteCollection(item.id, item.row_version),
    onSuccess: () => {
      setActive("");
      setIds([]);
      void client.invalidateQueries({ queryKey: ["collections"] });
    },
  });
  const removeMembers = useMutation({
    mutationFn: () => api.collectionMembers(active, ids, true),
    onSuccess: (result) => {
      setIds(
        result.items
          .filter((item) => item.status === "failed")
          .map((item) => item.id),
      );
      void client.invalidateQueries({ queryKey: ["collections"] });
      void client.invalidateQueries({
        queryKey: ["collection-members", active],
      });
    },
  });
  return (
    <div className="page">
      <PageHeader
        title="收藏集合"
        eyebrow="个人资料整理"
        description="把同一课题、阅读计划或实验相关的资料放在一起。"
        actions={
          <button
            className="button primary"
            type="button"
            onClick={() => {
              setEditing(null);
              setLabel("");
              setDescription("");
              setNewForm(true);
            }}
          >
            新建集合
          </button>
        }
      />
      {(newForm || editing) && (
        <form
          className="section-block"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <h2>{editing ? "编辑集合" : "新建集合"}</h2>
          <div className="form-row">
            <label className="grow">
              <span>名称 *</span>
              <input
                required
                value={label}
                onChange={(e) => setLabel(e.target.value)}
              />
            </label>
            <label className="grow">
              <span>说明</span>
              <input
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </label>
          </div>
          <div className="button-row">
            <button
              className="button primary compact"
              disabled={save.isPending || !label.trim()}
            >
              保存
            </button>
            <button
              className="button secondary compact"
              type="button"
              onClick={() => {
                setEditing(null);
                setNewForm(false);
              }}
            >
              取消
            </button>
          </div>
          {save.isError && (
            <p className="inline-error" role="alert">
              {getApiErrorMessage(save.error)}
            </p>
          )}
        </form>
      )}
      {list.isPending ? (
        <LoadingState />
      ) : list.isError ? (
        <ErrorState error={list.error} retry={() => void list.refetch()} />
      ) : list.data.items.length ? (
        <div className="collections-layout">
          <aside className="section-block collection-list">
            {list.data.items.map((item) => (
              <article
                key={item.id}
                className={active === item.id ? "is-active" : ""}
              >
                <button
                  className="collection-open"
                  type="button"
                  onClick={() => {
                    setActive(item.id);
                    setIds([]);
                  }}
                >
                  <strong>{item.label ?? item.title ?? item.name}</strong>
                  <span>
                    {item.member_count ?? 0} 项 · {item.description ?? ""}
                  </span>
                </button>
                <div className="button-row">
                  <button
                    className="button ghost compact"
                    type="button"
                    onClick={() => {
                      setEditing(item);
                      setLabel(item.label ?? item.title ?? item.name ?? "");
                      setDescription(item.description ?? "");
                    }}
                  >
                    编辑
                  </button>
                  <button
                    className="button ghost compact"
                    type="button"
                    disabled={remove.isPending}
                    onClick={() => {
                      if (window.confirm("删除这个集合？集合内的资料会保留。"))
                        remove.mutate(item);
                    }}
                  >
                    删除
                  </button>
                </div>
              </article>
            ))}
          </aside>
          <section>
            {active ? (
              <>
                <SelectionToolbar ids={ids} clear={() => setIds([])} />
                {!!ids.length && (
                  <button
                    className="button secondary compact"
                    disabled={removeMembers.isPending}
                    onClick={() => removeMembers.mutate()}
                  >
                    将所选资料移出集合
                  </button>
                )}
                {members.isPending ? (
                  <LoadingState label="正在加载集合…" />
                ) : members.isError ? (
                  <ErrorState error={members.error} />
                ) : members.data.pages.some((page) => page.items.length) ? (
                  <div className="asset-list">
                    {members.data.pages
                      .flatMap((page) => page.items)
                      .map((asset) => (
                        <div className="selectable-asset" key={asset.id}>
                          <label className="asset-select">
                            <input
                              type="checkbox"
                              checked={ids.includes(asset.id)}
                              onChange={(e) =>
                                setIds((current) =>
                                  e.target.checked
                                    ? [...current, asset.id]
                                    : current.filter((id) => id !== asset.id),
                                )
                              }
                            />
                            <span className="sr-only">选择 {asset.title}</span>
                          </label>
                          <AssetCard asset={asset} />
                        </div>
                      ))}
                  </div>
                ) : (
                  <EmptyState
                    title="集合还没有资料"
                    detail="从搜索结果勾选资料，或在资料详情加入这个集合。"
                  />
                )}
              </>
            ) : (
              <EmptyState
                title="选择一个集合"
                detail="点击左侧集合查看资料。"
              />
            )}
          </section>
        </div>
      ) : (
        <EmptyState
          title="还没有收藏集合"
          detail="新建一个集合，再从搜索结果或资料详情加入论文。"
        />
      )}
      {members.hasNextPage && (
        <div className="load-more">
          <button
            className="button secondary"
            disabled={members.isFetchingNextPage}
            onClick={() => void members.fetchNextPage()}
          >
            加载更多集合资料
          </button>
        </div>
      )}
      {removeMembers.data?.items
        .filter((item) => item.status === "failed")
        .map((item) => (
          <p className="inline-error" key={item.id} role="alert">
            {item.id}：{workflowItemError(item)}
          </p>
        ))}
      {(remove.isError || removeMembers.isError) && (
        <p className="inline-error" role="alert">
          {getApiErrorMessage(remove.error ?? removeMembers.error)}
        </p>
      )}
    </div>
  );
}
