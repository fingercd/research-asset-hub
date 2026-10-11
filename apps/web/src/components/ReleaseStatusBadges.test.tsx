import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Asset } from "../api/types";
import { releaseStatusForAsset } from "../lib/releaseStatus";
import { ReleaseStatusBadges } from "./ReleaseStatusBadges";

describe("ReleaseStatusBadges", () => {
  it("keeps code, local snapshot, and checkpoint states independent", () => {
    const asset: Asset = {
      id: "paper-1",
      title: "AgentVLN",
      attributes: {
        release_status: {
          code_open: "official_open",
          code_local: "stale",
          checkpoint_open: "announced",
          repository_url: "https://github.com/example/repo",
          verified_at: "2026-09-03",
        },
      },
    };

    expect(releaseStatusForAsset(asset)).toMatchObject({
      code_open: "official_open",
      code_local: "unknown",
      code_local_at_verification: "stale",
      checkpoint_open: "announced",
    });
    render(<ReleaseStatusBadges asset={asset} />);
    expect(screen.getByText("官方代码已开源")).toBeInTheDocument();
    expect(screen.getByText("核验时（2026-09-03）：本地快照已过期")).toBeInTheDocument();
    expect(screen.getByText("Checkpoint 待发布")).toBeInTheDocument();
    expect(screen.getByTitle("代码核验日期：2026-09-03")).toBeInTheDocument();
    expect(screen.getByTitle("Checkpoint 核验日期：2026-09-03")).toBeInTheDocument();
  });

  it("does not make a code-only audit look like a fresh checkpoint audit", () => {
    const asset: Asset = {
      id: "paper-code-update",
      title: "Paper with a later code release",
      attributes: {
        release_status: {
          code_open: "official_open",
          code_local: "full",
          checkpoint_open: "unknown",
          repository_url: "https://github.com/example/repo",
          verified_at: "2026-09-03",
          code_verified_at: "2026-09-12",
        },
      },
    };
    render(<ReleaseStatusBadges asset={asset} />);
    expect(screen.getByTitle("代码核验日期：2026-09-12")).toHaveAttribute(
      "href", "https://github.com/example/repo",
    );
    expect(screen.getByTitle("Checkpoint 核验日期：2026-09-03")).toHaveTextContent(
      "Checkpoint 待核验",
    );
  });

  it("distinguishes an official project page from a released implementation", () => {
    const asset: Asset = {
      id: "paper-placeholder",
      title: "Paper with a project-only repository",
      attributes: {
        release_status: {
          code_open: "official_placeholder",
          code_local: "absent",
          repository_url: "https://github.com/example/project-page",
          code_verified_at: "2026-09-12",
        },
      },
    };
    render(<ReleaseStatusBadges asset={asset} />);
    expect(screen.getByText("官方页面尚无实现")).toBeInTheDocument();
    expect(screen.getByText("核验时（2026-09-12）：未下载到本地")).toBeInTheDocument();
    expect(screen.queryByText("官方代码已开源")).not.toBeInTheDocument();
  });

  it("shows currently readable code separately from a dated absent audit", () => {
    const asset: Asset = {
      id: "stream-vln", title: "StreamVLN",
      attributes: { release_status: { code_local: "absent", verified_at: "2026-09-03" } },
      quick_actions: [{ kind: "code", status: "available", owner_asset_id: "repo" }],
    };
    render(<ReleaseStatusBadges asset={asset} />);
    expect(screen.getByText("本地代码可读")).toBeInTheDocument();
    expect(screen.queryByText("本地完整")).not.toBeInTheDocument();
    expect(screen.getByText("核验时（2026-09-03）：未下载到本地")).toBeInTheDocument();
  });

  it.each(["full", "partial", "stale"])("keeps %s as audit evidence when local code is absent", (codeLocal) => {
    const asset: Asset = {
      id: "paper", title: "Paper",
      attributes: { release_status: { code_local: codeLocal, verified_at: "2026-09-03" } },
      quick_actions: [{ kind: "code", status: "network_required", owner_asset_id: "repo" }],
    };
    expect(releaseStatusForAsset(asset)).toMatchObject({
      code_local: "absent", code_local_at_verification: codeLocal,
    });
  });
});
