import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Asset, ReadingStatus } from "../api/types";
import { invalidateResearchData } from "../lib/invalidateResearchData";

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
  return (
    <ReadingStatusControlForAsset
      key={assetId}
      assetId={assetId}
      value={value}
      compact={compact}
    />
  );
}

function ReadingStatusControlForAsset({
  assetId,
  value,
  compact = false,
}: {
  assetId: string;
  value: ReadingStatus;
  compact?: boolean;
}) {
  const queryClient = useQueryClient();
  const [confirmedStatus, setConfirmedStatus] = useState<ReadingStatus | null>(null);
  const previousValue = useRef(value);

  useEffect(() => {
    if (value !== previousValue.current) {
      previousValue.current = value;
      setConfirmedStatus(null);
    }
  }, [value]);

  const mutation = useMutation({
    mutationFn: (status: ReadingStatus) => api.updateReadingStatus(assetId, status),
    onSuccess: (result, status) => {
      const savedStatus =
        ("reading_status" in result && result.reading_status) || status;
      setConfirmedStatus(savedStatus);
      queryClient.setQueryData<Asset>(["asset", assetId], (current) =>
        current
          ? {
              ...current,
              reading_status: savedStatus,
              reading_state: { status: savedStatus },
            }
          : current,
      );
      invalidateResearchData(queryClient, { assetId });
    },
  });

  return (
    <div className={`status-select ${compact ? "is-compact" : ""}`}>
      <label>
        <span className="sr-only">阅读状态</span>
        <select
          aria-label="阅读状态"
          value={
            mutation.isPending
              ? mutation.variables ?? confirmedStatus ?? value
              : confirmedStatus ?? value
          }
          disabled={mutation.isPending}
          onChange={(event) => mutation.mutate(event.target.value as ReadingStatus)}
        >
          {(Object.keys(labels) as ReadingStatus[]).map((status) => (
            <option value={status} key={status}>
              {labels[status]}
            </option>
          ))}
        </select>
      </label>
      {mutation.isError && (
        <span className="inline-error" role="alert">
          保存失败{" "}
          <button
            className="button secondary compact"
            type="button"
            onClick={() => {
              if (mutation.variables) mutation.mutate(mutation.variables);
            }}
          >
            重试
          </button>
        </span>
      )}
    </div>
  );
}
