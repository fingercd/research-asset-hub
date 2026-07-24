import { AlertTriangle, ChevronLeft, ChevronRight } from "lucide-react";
import {
  GlobalWorkerOptions,
  getDocument,
  type PDFDocumentProxy,
  type RenderTask,
} from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { useEffect, useRef, useState } from "react";
import type { Representation } from "../api/types";
import { representationLabel } from "../lib/format";
import { useRepresentationUrl } from "../lib/useRepresentationUrl";
import { EmptyState, ErrorState, LoadingState } from "./PageState";
import { SafeOpenLink } from "./SafeOpenLink";

GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

export function PdfPanel({
  representation,
}: {
  representation?: Representation;
}) {
  const source = useRepresentationUrl(representation);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!source.data) return;
    let current = true;
    const task = getDocument({ url: source.data, withCredentials: true });
    setDocument(null);
    setPageNumber(1);
    setLoadError(null);
    void task.promise
      .then((loaded) => {
        if (current) setDocument(loaded);
      })
      .catch((error: unknown) => {
        if (current) {
          setLoadError(error instanceof Error ? error.message : "PDF 无法嵌入。");
        }
      });
    return () => {
      current = false;
      void task.destroy();
    };
  }, [source.data]);

  useEffect(() => {
    if (!document || !canvasRef.current) return;
    let current = true;
    let renderTask: RenderTask | undefined;
    void document.getPage(pageNumber).then((page) => {
      if (!current || !canvasRef.current) return;
      const viewport = page.getViewport({ scale: 1.45 });
      const canvas = canvasRef.current;
      const context = canvas.getContext("2d");
      if (!context) return;
      canvas.width = Math.ceil(viewport.width);
      canvas.height = Math.ceil(viewport.height);
      renderTask = page.render({ canvas, canvasContext: context, viewport });
      return renderTask.promise;
    });
    return () => {
      current = false;
      renderTask?.cancel();
    };
  }, [document, pageNumber]);

  if (!representation) {
    return <EmptyState title="暂无 PDF" detail="该资产没有登记可用的 PDF 表示。" />;
  }
  if (source.isPending) return <LoadingState label="正在创建 PDF 安全会话…" />;
  if (source.isError || !source.data) {
    return <ErrorState error={source.error ?? new Error("未返回可打开地址。")} />;
  }

  return (
    <section className="document-viewer" aria-label="PDF 阅读器">
      <header>
        <div>
          <strong>{representationLabel(representation)}</strong>
          <span>pdf.js 本地阅读视图</span>
        </div>
        <SafeOpenLink url={source.data} label="在新窗口打开 PDF" />
      </header>
      {loadError ? (
        <div className="viewer-fallback" role="status">
          <AlertTriangle size={20} aria-hidden="true" />
          <div>
            <strong>浏览器未能内嵌这份 PDF</strong>
            <span>
              可能是文件损坏、加密或网关会话受限；可使用上方按钮安全打开原文件。
            </span>
          </div>
        </div>
      ) : document ? (
        <>
          <div className="pdf-toolbar" aria-label="PDF 翻页">
            <button
              className="button secondary compact"
              type="button"
              disabled={pageNumber <= 1}
              onClick={() => setPageNumber((value) => Math.max(1, value - 1))}
            >
              <ChevronLeft size={14} aria-hidden="true" /> 上一页
            </button>
            <span>
              第 {pageNumber} / {document.numPages} 页
            </span>
            <button
              className="button secondary compact"
              type="button"
              disabled={pageNumber >= document.numPages}
              onClick={() =>
                setPageNumber((value) => Math.min(document.numPages, value + 1))
              }
            >
              下一页 <ChevronRight size={14} aria-hidden="true" />
            </button>
          </div>
          <div className="pdf-canvas-wrap">
            <canvas ref={canvasRef} aria-label={`PDF 第 ${pageNumber} 页`} />
          </div>
        </>
      ) : (
        <LoadingState label="正在解析 PDF…" />
      )}
    </section>
  );
}
