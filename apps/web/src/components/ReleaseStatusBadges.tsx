import { ExternalLink } from "lucide-react";
import type { Asset, QuickAction } from "../api/types";
import {
  releaseStatusForAsset,
  type CheckpointOpenStatus,
  type CodeLocalStatus,
  type CodeOpenStatus,
} from "../lib/releaseStatus";

const codeLabels: Record<CodeOpenStatus, string> = {
  official_open: "官方代码已开源",
  official_announced: "官方代码待发布",
  official_placeholder: "官方页面尚无实现",
  unavailable: "代码入口不可用",
  third_party_only: "仅第三方实现",
  unknown: "代码公开状态未确认",
};

const localLabels: Record<CodeLocalStatus, string> = {
  available: "本地代码可读",
  full: "本地完整",
  partial: "本地部分",
  absent: "未下载到本地",
  stale: "本地快照已过期",
  unknown: "本地状态待核验",
};

const checkpointLabels: Record<CheckpointOpenStatus, string> = {
  open: "Checkpoint 已公开",
  announced: "Checkpoint 待发布",
  not_found: "未发现 Checkpoint",
  not_applicable: "无需专属 Checkpoint",
  unknown: "Checkpoint 待核验",
};

export function ReleaseStatusBadges({
  asset,
  quickActions,
  relatedRepositories,
}: {
  asset: Asset;
  quickActions?: QuickAction[];
  relatedRepositories?: Asset[];
}) {
  const status = releaseStatusForAsset(
    asset,
    quickActions,
    relatedRepositories,
  );
  const repositoryContent = (
    <>
      <strong>代码</strong>
      <span>{codeLabels[status.code_open]}</span>
      <small>
        {status.code_local === "absent" ? "当前无可用本地代码" : localLabels[status.code_local]}
      </small>
      {status.code_local_at_verification !== "unknown" && (
        <small>
          核验时（{status.code_verified_at ?? "日期未记录"}）：{localLabels[status.code_local_at_verification]}
        </small>
      )}
    </>
  );
  const checkpointContent = (
    <>
      <strong>Checkpoint</strong>
      <span>{checkpointLabels[status.checkpoint_open]}</span>
    </>
  );
  const codeDate = status.code_verified_at
    ? `代码核验日期：${status.code_verified_at}`
    : undefined;
  const checkpointDate = status.checkpoint_verified_at
    ? `Checkpoint 核验日期：${status.checkpoint_verified_at}`
    : undefined;
  return (
    <div
      className="release-status-row"
      aria-label="代码与 Checkpoint 发布状态"
    >
      {status.repository_url ? (
        <a href={status.repository_url} target="_blank" rel="noreferrer" title={codeDate}>
          {repositoryContent}
          <ExternalLink size={13} aria-hidden="true" />
        </a>
      ) : (
        <div title={codeDate}>{repositoryContent}</div>
      )}
      {status.checkpoint_urls[0] ? (
        <a href={status.checkpoint_urls[0]} target="_blank" rel="noreferrer" title={checkpointDate}>
          {checkpointContent}
          <ExternalLink size={13} aria-hidden="true" />
        </a>
      ) : (
        <div title={checkpointDate}>{checkpointContent}</div>
      )}
    </div>
  );
}
