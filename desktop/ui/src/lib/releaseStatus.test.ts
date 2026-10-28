import { describe, expect, it } from "vitest";
import type { Asset, QuickActionStatus } from "../api/types";
import { codeSummaryForAsset, releaseStatusForAsset } from "./releaseStatus";

const paper: Asset = {
  id: "paper", title: "Paper", source_urls: ["https://arxiv.org/abs/1234.5678"],
  related_repositories: [{
    id: "repo", title: "Official Repo", source_urls: ["https://github.com/example/repo"],
    representations: [{ id: "code", representation_type: "repository", availability: "available", materialization_kind: "directory", path_ref: "repo" }],
  }],
};

describe("code availability summaries", () => {
  it.each([
    ["available", "本地可读：Official Repo", "available"],
    ["network_required", "远程代码，需联网：Official Repo", "absent"],
    ["missing", "已登记代码当前不可用：Official Repo", "absent"],
    ["not_provided", "无已确认代码：Official Repo", "absent"],
  ] as const)("uses authoritative %s action rather than old local paths", (status: QuickActionStatus, summary, localStatus) => {
    const asset = { ...paper, quick_actions: [{ kind: "code" as const, status, owner_asset_id: "repo", representation_id: "code" }] };
    expect(codeSummaryForAsset(asset)).toBe(summary);
    expect(releaseStatusForAsset(asset).code_local).toBe(localStatus);
  });

  it("can summarize related representations without a quick action", () => {
    expect(codeSummaryForAsset(paper)).toBe("本地可读：repository");
    expect(releaseStatusForAsset(paper).code_local).toBe("available");
  });

  it("does not infer local code from a repository URL", () => {
    const asset: Asset = { id: "remote", title: "Remote", source_urls: ["https://github.com/example/remote"] };
    expect(codeSummaryForAsset(asset)).toContain("本地状态待核验");
    expect(releaseStatusForAsset(asset).code_local).toBe("unknown");
  });
});
