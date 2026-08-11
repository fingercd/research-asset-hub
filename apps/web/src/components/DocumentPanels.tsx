import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import { getRepresentationType, type Representation } from "../api/types";
import { representationLabel } from "../lib/format";
import { useRepresentationUrl } from "../lib/useRepresentationUrl";
import { EmptyState, ErrorState, LoadingState } from "./PageState";
import { SafeOpenLink } from "./SafeOpenLink";
import { CodeBrowser } from "./CodeBrowser";

function IsolatedFrame({
  representation,
  title,
  sandbox,
}: {
  representation?: Representation;
  title: string;
  sandbox: string;
}) {
  const source = useRepresentationUrl(representation);
  if (!representation) {
    return <EmptyState title={`暂无${title}`} detail={`该资产没有登记可用的${title}表示。`} />;
  }
  if (source.isPending) return <LoadingState label={`正在创建${title}安全会话…`} />;
  if (source.isError || !source.data) {
    return <ErrorState error={source.error ?? new Error("未返回可打开地址。")} />;
  }
  return (
    <section className="document-viewer">
      <header>
        <div>
          <strong>{representationLabel(representation)}</strong>
          <span>{representation.network_required ? "需要联网资源" : "隔离本地视图"}</span>
        </div>
        <SafeOpenLink url={source.data} label={`在新窗口打开${title}`} />
      </header>
      <iframe
        className="document-frame"
        src={source.data}
        title={title}
        sandbox={sandbox}
        referrerPolicy="no-referrer"
      />
    </section>
  );
}

function MarkdownPanel({ representation }: { representation: Representation }) {
  const source = useRepresentationUrl(representation);
  const markdown = useQuery({
    queryKey: ["representation-markdown", representation.id, source.data],
    queryFn: async () => {
      const response = await fetch(source.data!, {
        credentials: "include",
        headers: { Accept: "text/markdown,text/plain;q=0.9" },
      });
      if (!response.ok) throw new Error(`Markdown 返回 HTTP ${response.status}`);
      return response.text();
    },
    enabled: Boolean(source.data),
  });

  if (source.isPending || markdown.isPending) {
    return <LoadingState label="正在加载导读…" />;
  }
  if (source.isError || !source.data || markdown.isError) {
    return (
      <IsolatedFrame
        representation={representation}
        title="导读"
        sandbox="allow-same-origin"
      />
    );
  }
  return (
    <section className="document-viewer">
      <header>
        <div>
          <strong>{representationLabel(representation)}</strong>
          <span>安全 Markdown 阅读视图</span>
        </div>
        <SafeOpenLink url={source.data} label="打开原始导读" />
      </header>
      <article className="markdown-view">
        <ReactMarkdown rehypePlugins={[rehypeSanitize]}>{markdown.data}</ReactMarkdown>
      </article>
    </section>
  );
}

export function GuidePanel({
  representation,
}: {
  representation?: Representation;
}) {
  if (!representation) {
    return <EmptyState title="暂无导读" detail="该资产没有登记导读、分析或 Markdown 表示。" />;
  }
  const type = getRepresentationType(representation);
  const markdown =
    type === "markdown" || representation.mime_type?.includes("markdown");
  return markdown ? (
    <MarkdownPanel representation={representation} />
  ) : (
    <IsolatedFrame representation={representation} title="导读" sandbox="" />
  );
}

export function CodePanel({
  representation,
}: {
  representation?: Representation;
}) {
  if (!representation) {
    return <EmptyState title="暂无代码" detail="该资产没有登记代码仓库、源代码或 Notebook。" />;
  }
  return (
    <CodeBrowser
      representationId={representation.id}
      repositoryLabel={representationLabel(representation)}
    />
  );
}

export function HtmlPanel({
  representation,
}: {
  representation?: Representation;
}) {
  const policy = String(representation?.attributes?.sandbox_policy ?? "static");
  const sandbox =
    policy === "trusted_local"
      ? "allow-scripts allow-forms"
      : policy === "network_required" || representation?.network_required
        ? "allow-scripts allow-forms"
        : "";
  return (
    <IsolatedFrame
      representation={representation}
      title="原始 HTML"
      sandbox={sandbox}
    />
  );
}
