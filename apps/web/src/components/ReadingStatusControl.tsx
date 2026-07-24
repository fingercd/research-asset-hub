import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Asset, ReadingStatus } from "../api/types";

const labels: Record<ReadingStatus, string> = {
  unread: "未读",
  reading: "阅读中",
  read: "已读",
};

export function ReadingStatusControl({
  assetId,
  value,
  compact = false,
}: {
  assetId: string;
  value: ReadingStatus;
  compact?: boolean;
}) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (status: ReadingStatus) => api.updateReadingStatus(assetId, status),
    onSuccess: (_data, status) => {
      queryClient.setQueryData<Asset>(["asset", assetId], (current) =>
        current
          ? { ...current, reading_status: status, reading_state: { status } }
          : current,
      );
      void queryClient.invalidateQueries({ queryKey: ["search"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      void queryClient.invalidateQueries({ queryKey: ["assets"] });
    },
  });

  return (
    <label className={`status-select ${compact ? "is-compact" : ""}`}>
      <span className="sr-only">阅读状态</span>
      <select
        aria-label="阅读状态"
        value={mutation.variables ?? value}
        disabled={mutation.isPending}
        onChange={(event) => mutation.mutate(event.target.value as ReadingStatus)}
      >
        {(Object.keys(labels) as ReadingStatus[]).map((status) => (
          <option value={status} key={status}>
            {labels[status]}
          </option>
        ))}
      </select>
      {mutation.isError && <span className="inline-error">保存失败</span>}
    </label>
  );
}
