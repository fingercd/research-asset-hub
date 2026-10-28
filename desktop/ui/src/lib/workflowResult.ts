import type { WorkflowItemResult } from "../api/types";
export function workflowItemError(item: WorkflowItemResult): string {
  return typeof item.error === "string"
    ? item.error
    : (item.error?.message ?? "该项未能保存，请刷新后重试。");
}
