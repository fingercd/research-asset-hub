import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ChevronLeft, ChevronRight } from "lucide-react";
import {
  GlobalWorkerOptions,
  getDocument,
  TextLayer,
  type PDFDocumentProxy,
  type RenderTask,
} from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { useEffect, useRef, useState } from "react";
import { api, getApiErrorMessage } from "../api/client";
import type {
  Annotation,
  PdfAnchor,
  ReadingPositionResponse,
  Representation,
} from "../api/types";
import { representationLabel } from "../lib/format";
import { useRepresentationUrl } from "../lib/useRepresentationUrl";
import { EmptyState, ErrorState, LoadingState } from "./PageState";
import { SafeOpenLink } from "./SafeOpenLink";
GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

export function PdfPanel({
  representation,
  assetId,
  initialPage,
  annotations = [],
}: {
  representation?: Representation;
  assetId?: string;
  initialPage?: number;
  annotations?: Annotation[];
}) {
  const client = useQueryClient();
  const source = useRepresentationUrl(representation);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textRef = useRef<HTMLDivElement>(null);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const [pdfDocument, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [pageNumber, setPageNumber] = useState(initialPage ?? 1);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [renderError, setRenderError] = useState<string | null>(null);
  const [selection, setSelection] = useState<PdfAnchor | null>(null);
  const [note, setNote] = useState("");
  const [size, setSize] = useState({ width: 800, height: 1000 });
  const [positionError, setPositionError] = useState("");
  const restored = useRef(false);
  const positionRef = useRef<ReadingPositionResponse | null>(null);
  const saveQueue = useRef<Promise<void>>(Promise.resolve());
  const position = useQuery({
    queryKey: ["reading-position", assetId],
    queryFn: ({ signal }) => api.readingPosition(assetId!, signal),
    enabled: !!assetId,
    retry: false,
  });
  positionRef.current = position.data ?? null;
  const highlight = useMutation({
    mutationFn: () =>
      api.createAnnotation({
        asset_id: assetId!,
        representation_id: representation!.id,
        kind: "highlight",
        anchor_kind: "pdf_page",
        anchor: selection,
        body_markdown: note.trim() || selection?.quote || "",
      }),
    onSuccess: () => {
      setSelection(null);
      setNote("");
      window.getSelection()?.removeAllRanges();
      void client.invalidateQueries({ queryKey: ["annotations", assetId] });
    },
  });
  useEffect(() => {
    restored.current = false;
    setSelection(null);
    setPageNumber(1);
  }, [representation?.id]);
  useEffect(() => {
    if (!source.data) return;
    let current = true;
    const task = getDocument({ url: source.data, withCredentials: true });
    setDocument(null);
    setLoadError(null);
    void task.promise
      .then((loaded) => {
        if (current) setDocument(loaded);
      })
      .catch((error: unknown) => {
        if (current) setLoadError(getApiErrorMessage(error));
      });
    return () => {
      current = false;
      void task.destroy();
    };
  }, [source.data]);
  useEffect(() => {
    if (!pdfDocument || restored.current || (position.isPending && assetId))
      return;
    restored.current = true;
    const saved = position.data?.position;
    const target =
      initialPage ??
      (!position.data?.stale &&
      saved &&
      saved.representation_id === representation?.id
        ? saved.page
        : 1);
    setPageNumber(Math.max(1, Math.min(pdfDocument.numPages, target)));
  }, [
    pdfDocument,
    position.isPending,
    position.data,
    representation?.id,
    assetId,
    initialPage,
  ]);
  useEffect(() => {
    if (initialPage && pdfDocument)
      setPageNumber(Math.max(1, Math.min(pdfDocument.numPages, initialPage)));
  }, [initialPage, pdfDocument]);
  useEffect(() => {
    if (!pdfDocument || !canvasRef.current || !textRef.current) return;
    let current = true;
    let renderTask: RenderTask | undefined;
    let textLayer: TextLayer | undefined;
    setSelection(null);
    setRenderError(null);
    void pdfDocument
      .getPage(pageNumber)
      .then(async (page) => {
        const textContent = await page.getTextContent();
        if (!current || !canvasRef.current || !textRef.current) return;
        const available = surfaceRef.current?.parentElement?.clientWidth ?? 800;
        const base = page.getViewport({ scale: 1 });
        const viewport = page.getViewport({
          scale: Math.min(1.45, (available - 32) / base.width),
        });
        const canvas = canvasRef.current;
        const context = canvas.getContext("2d");
        if (!context) return;
        setSize({ width: viewport.width, height: viewport.height });
        canvas.width = Math.ceil(viewport.width);
        canvas.height = Math.ceil(viewport.height);
        textRef.current.replaceChildren();
        textRef.current.style.setProperty(
          "--total-scale-factor",
          String(viewport.scale),
        );
        textRef.current.style.setProperty("--scale-round-x", "1px");
        textRef.current.style.setProperty("--scale-round-y", "1px");
        renderTask = page.render({ canvas, canvasContext: context, viewport });
        textLayer = new TextLayer({
          textContentSource: textContent,
          container: textRef.current,
          viewport,
        });
        await Promise.all([renderTask.promise, textLayer.render()]);
      })
      .catch((error) => {
        if (
          current &&
          error?.name !== "RenderingCancelledException" &&
          error?.name !== "AbortException"
        )
          setRenderError(getApiErrorMessage(error));
      });
    return () => {
      current = false;
      renderTask?.cancel();
      textLayer?.cancel();
    };
  }, [pdfDocument, pageNumber]);
  useEffect(() => {
    if (
      !assetId ||
      !representation ||
      !pdfDocument ||
      !restored.current ||
      position.isPending ||
      position.isError
    )
      return;
    const timer = window.setTimeout(() => {
      saveQueue.current = saveQueue.current.then(async () => {
        const previous = positionRef.current?.position;
        try {
          const result = await api.saveReadingPosition(
            assetId,
            {
              representation_id: representation.id,
              page: pageNumber,
              source_hash: representation.sha256,
            },
            previous?.row_version,
          );
          positionRef.current = result;
          client.setQueryData(["reading-position", assetId], result);
          setPositionError("");
        } catch (error) {
          setPositionError(getApiErrorMessage(error));
        }
      });
    }, 750);
    return () => window.clearTimeout(timer);
  }, [
    assetId,
    representation,
    pdfDocument,
    pageNumber,
    position.isPending,
    position.isError,
    client,
  ]);
  function captureSelection() {
    const selected = window.getSelection();
    const surface = surfaceRef.current;
    if (!selected?.rangeCount || !surface || !representation || !assetId)
      return;
    const range = selected.getRangeAt(0);
    if (!textRef.current?.contains(range.commonAncestorContainer)) return;
    const quote = selected.toString().trim();
    if (!quote) return;
    const bounds = surface.getBoundingClientRect();
    const rects = Array.from(range.getClientRects())
      .filter((r) => r.width > 0 && r.height > 0)
      .map((r) => ({
        x: Math.max(0, (r.left - bounds.left) / bounds.width),
        y: Math.max(0, (r.top - bounds.top) / bounds.height),
        width: Math.min(1, r.width / bounds.width),
        height: Math.min(1, r.height / bounds.height),
      }));
    setSelection({
      representation_id: representation.id,
      source_hash: representation.sha256,
      page: pageNumber,
      rects,
      quote,
    });
  }
  if (!representation)
    return (
      <EmptyState title="暂无 PDF" detail="该资产没有登记可用的 PDF 表示。" />
    );
  if (source.isPending) return <LoadingState label="正在创建 PDF 安全会话…" />;
  if (source.isError || !source.data)
    return (
      <ErrorState error={source.error ?? new Error("未返回可打开地址。")} />
    );
  const anchored = annotations.filter(
    (a) =>
      a.representation_id === representation.id &&
      a.anchor?.page === pageNumber,
  );
  const stale = anchored.filter(
    (a) =>
      a.anchor?.source_hash &&
      representation.sha256 &&
      a.anchor.source_hash !== representation.sha256,
  );
  return (
    <section className="document-viewer" aria-label="PDF 阅读器">
      <header>
        <div>
          <strong>{representationLabel(representation)}</strong>
          <span>选中文字可保存高亮，阅读页码自动保存。</span>
        </div>
        <SafeOpenLink url={source.data} label="在新窗口打开 PDF" />
      </header>
      {position.data?.stale && (
        <p className="inline-warning">
          这份文件已变化，上次阅读位置可能失效：{position.data.reason}
        </p>
      )}
      {(positionError || position.isError) && (
        <p className="inline-warning" role="status">
          阅读位置未保存：{positionError || getApiErrorMessage(position.error)}
        </p>
      )}
      {loadError ? (
        <div className="viewer-fallback">
          <AlertTriangle size={20} />
          <strong>{loadError}</strong>
          <span>可使用上方按钮打开原文件。</span>
        </div>
      ) : pdfDocument ? (
        <>
          <div className="pdf-toolbar" aria-label="PDF 翻页">
            <button
              className="button secondary compact"
              disabled={pageNumber <= 1}
              onClick={() => setPageNumber((p) => p - 1)}
            >
              <ChevronLeft size={14} />
              上一页
            </button>
            <label className="pdf-page-input">
              第{" "}
              <input
                aria-label="跳转到页码"
                type="number"
                min="1"
                max={pdfDocument.numPages}
                value={pageNumber}
                onChange={(e) => {
                  const value = Number(e.target.value);
                  if (value >= 1 && value <= pdfDocument.numPages)
                    setPageNumber(value);
                }}
              />{" "}
              / {pdfDocument.numPages} 页
            </label>
            <button
              className="button secondary compact"
              disabled={pageNumber >= pdfDocument.numPages}
              onClick={() => setPageNumber((p) => p + 1)}
            >
              下一页
              <ChevronRight size={14} />
            </button>
          </div>
          {renderError && (
            <p className="inline-error" role="alert">
              {renderError}
            </p>
          )}
          {!!stale.length && (
            <p className="inline-warning">
              本页 {stale.length}{" "}
              条高亮来自旧版本，已隐藏其标记。摘录内容仍保留。
            </p>
          )}
          {selection && (
            <div className="pdf-selection-composer">
              <p>“{selection.quote}”</p>
              <label>
                <span>批注（可选）</span>
                <input value={note} onChange={(e) => setNote(e.target.value)} />
              </label>
              <div className="button-row">
                <button
                  className="button primary compact"
                  disabled={highlight.isPending}
                  onClick={() => highlight.mutate()}
                >
                  保存高亮
                </button>
                <button
                  className="button secondary compact"
                  onClick={() => setSelection(null)}
                >
                  取消
                </button>
              </div>
              {highlight.isError && (
                <p className="inline-error" role="alert">
                  {getApiErrorMessage(highlight.error)}
                </p>
              )}
            </div>
          )}
          <div className="pdf-canvas-wrap">
            <div
              ref={surfaceRef}
              className="pdf-page-surface"
              style={{ width: size.width, height: size.height }}
              onMouseUp={captureSelection}
            >
              <canvas ref={canvasRef} aria-label={`PDF 第 ${pageNumber} 页`} />
              <div ref={textRef} className="textLayer" />
              <div className="pdf-highlights" aria-hidden="true">
                {anchored
                  .filter((a) => !stale.includes(a))
                  .flatMap((a) =>
                    (a.anchor?.rects ?? []).map((rect, i) => (
                      <mark
                        key={`${a.id}-${i}`}
                        style={{
                          left: `${rect.x * 100}%`,
                          top: `${rect.y * 100}%`,
                          width: `${rect.width * 100}%`,
                          height: `${rect.height * 100}%`,
                        }}
                      />
                    )),
                  )}
              </div>
            </div>
          </div>
        </>
      ) : (
        <LoadingState label="正在解析 PDF…" />
      )}
    </section>
  );
}
