import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, getApiErrorMessage } from "../api/client";
import { workflowItemError } from "../lib/workflowResult";
export function AssetCollectionControl({ assetId }: { assetId: string }) {
  const client = useQueryClient();
  const [id, setId] = useState("");
  const list = useQuery({
    queryKey: ["collections"],
    queryFn: ({ signal }) => api.collections(signal),
  });
  const add = useMutation({
    mutationFn: () => api.collectionMembers(id, [assetId]),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["collections"] });
      void client.invalidateQueries({ queryKey: ["collection-members", id] });
    },
  });
  return (
    <div className="asset-collection-control">
      <select
        aria-label="选择收藏集合"
        value={id}
        onChange={(e) => {
          setId(e.target.value);
          add.reset();
        }}
      >
        <option value="">选择收藏集合</option>
        {list.data?.items.map((c) => (
          <option key={c.id} value={c.id}>
            {c.label ?? c.name ?? c.title}
          </option>
        ))}
      </select>
      <button
        className="button secondary compact"
        type="button"
        disabled={!id || add.isPending}
        onClick={() => add.mutate()}
      >
        {add.isSuccess && !add.data.failed ? "已加入" : "加入集合"}
      </button>
      {add.data?.items
        .filter((item) => item.status === "failed")
        .map((item) => (
          <span key={item.id} className="inline-error" role="alert">
            {workflowItemError(item)}
          </span>
        ))}
      {add.isError && (
        <span className="inline-error" role="alert">
          {getApiErrorMessage(add.error)}
        </span>
      )}
    </div>
  );
}
