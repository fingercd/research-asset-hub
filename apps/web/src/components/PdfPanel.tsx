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
import { api, ApiError, getApiErrorMessage } from "../api/client";
import type {
  Annotation,
  PdfAnchor,
  ReadingPositionResponse,
  Representation,
} from "../api/types";
import { pdfRepresentationLabel } from "../lib/pdfRepresentationLabel";
import "../styles/reading-workspace.css";
import { useRepresentationUrl } from "../lib/useRepresentationUrl";
import { EmptyState, LoadingState } from "./PageState";
import { SafeOpenLink } from "./SafeOpenLink";
GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

function pdfFailure(error: unknown): string {
  const detail = getApiErrorMessage(error);
  const status = error instanceof ApiError ? error.status : undefined;
  if (status === 401 || status === 403 || /401|403|expired|unauthorized|forbidden/i.test(detail))
    return "文件读取被拒绝或安全会话已过期。请重新创建安全会话后重试。";
  if (status === 404 || /404|missingpdf|not found/i.test(detail))
    return "未找到已登记的 PDF 文件。请在概览中核对文件与版本；不会自动改用外网文件。";
  if (/invalidpdf|invalid pdf|format|corrupt/i.test(detail))
    return "文件无法解析为 PDF，可能不完整或格式损坏。请核对这个版本的原文件。";
  if (/fetch|network|load failed/i.test(detail))
    return "无法取得 PDF 文件：可能是安全会话、网关连接或网络访问问题；当前无法据此判断文件是否存在。";
  return `PDF 加载失败：${detail}`;
}

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
  const [loadRevision, setLoadRevision] = useState(0);
  const [zoom, setZoom] = useState<number | "fit">("fit");
  const [containerWidth, setContainerWidth] = useState(800);
  const [positionReady, setPositionReady] = useState(false);
  const [saveRevision, setSaveRevision] = useState(0);
  const restored = useRef(false);
  const saveQueue = useRef<Promise<void>>(Promise.resolve());
  const saveBlocked = useRef(false);
  const lastSavedPage = useRef(1);
  const position = useQuery({
    queryKey: ["reading-position", assetId],
    queryFn: ({ signal }) => api.readingPosition(assetId!, signal),
    enabled: !!assetId,
    retry: false,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
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
    setPositionReady(false);
    setSelection(null);
    setPageNumber(1);
  }, [representation?.id]);
  useEffect(() => {
    if (!source.data) return;
    let current = true;
    const task = getDocument({ url: source.data.url, withCredentials: true });
    setDocument(null);
    setLoadError(null);
    void task.promise
      .then((loaded) => {
        if (current) setDocument(loaded);
      })
      .catch((error: unknown) => {
        if (current) setLoadError(pdfFailure(error));
      });
    return () => {
      current = false;
      void task.destroy();
    };
  }, [source.data, loadRevision]);
  useEffect(() => {
    if (!pdfDocument || restored.current || (position.isPending && assetId))
      return;
    restored.current = true;
    const saved = position.data?.position;
    const target =
      initialPage ??
      (!position.data?.stale &&
      saved &&
      saved.representation_id === representation?.id &&
      saved.source_hash && representation?.sha256 &&
      saved.source_hash === representation.sha256
        ? saved.page
        : 1);
    const restoredPage = Math.max(1, Math.min(pdfDocument.numPages, target));
    lastSavedPage.current = restoredPage;
    setPageNumber(restoredPage);
    setPositionReady(true);
  }, [
    pdfDocument,
    position.isPending,
    position.data,
    representation?.id,
    representation?.sha256,
    assetId,
    initialPage,
  ]);
  useEffect(() => {
    if (initialPage && pdfDocument)
      setPageNumber(Math.max(1, Math.min(pdfDocument.numPages, initialPage)));
  }, [initialPage, pdfDocument]);
  useEffect(() => {
    const container = surfaceRef.current?.parentElement;
    if (!container || !pdfDocument || !positionReady) return;
    const update = () => setContainerWidth(container.clientWidth || 800);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(container);
    return () => observer.disconnect();
  }, [pdfDocument, positionReady]);
  useEffect(() => {
    if (!pdfDocument || !positionReady || !canvasRef.current || !textRef.current) return;
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
        const base = page.getViewport({ scale: 1 });
        const viewport = page.getViewport({
          scale: zoom === "fit" ? Math.max(0.1, (containerWidth - 32) / base.width) : zoom,
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
          setRenderError(pdfFailure(error));
      });
    return () => {
      current = false;
      renderTask?.cancel();
      textLayer?.cancel();
    };
  }, [pdfDocument, pageNumber, zoom, containerWidth, positionReady]);
  useEffect(() => {
    if (
      !assetId ||
      !representation ||
      !pdfDocument ||
      !positionReady ||
      pageNumber === lastSavedPage.current ||
      position.isPending ||
      position.isError
    )
      return;
    const timer = window.setTimeout(() => {
      saveQueue.current = saveQueue.current.then(async () => {
        if (saveBlocked.current) return;
        const previous = client.getQueryData<ReadingPositionResponse>(["reading-position", assetId])?.position;
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
          lastSavedPage.current = pageNumber;
          client.setQueryData(["reading-position", assetId], result);
          setPositionError("");
        } catch (error) {
          // Never overwrite a newer reader after an ETag conflict. Queued writes
          // stop until the user explicitly refreshes the saved version.
          saveBlocked.current = true;
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
    positionReady,
    saveRevision,
    client,
  ]);
  function captureSelection() {
    const selected = window.getSelection();
    const surface = surfaceRef.current;
    if (!selected?.rangeCount || !surface || !representation?.sha256 || !assetId)
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
      <section className="viewer-fallback" role="alert">
        <strong>无法创建 PDF 安全会话</strong>
        <p>{pdfFailure(source.error ?? new Error("未返回可打开地址。"))}</p>
        <button className="button secondary" onClick={() => void source.refetch()}>重试安全会话</button>
        <small>不会绕过本地读取检查或自动切换到外网文件。</small>
      </section>
    );
  const anchored = annotations.filter(
    (a) =>
      a.representation_id === representation.id &&
      a.anchor?.page === pageNumber,
  );
  const stale = anchored.filter(
    (a) =>
      !a.anchor?.source_hash || !representation.sha256 ||
      a.anchor.source_hash !== representation.sha256,
  );
  return (
    <section className="document-viewer" aria-label="PDF 阅读器">
      <header>
        <div>
          <strong>{pdfRepresentationLabel(representation)}</strong>
          <span>{source.data.kind === "external_url" ? "仅登记了外网文件，需要联网读取。" : "经安全会话读取已登记文件；不会自动切换外网版本。"}选中文字可保存高亮，翻页后自动保存位置。</span>
        </div>
        <SafeOpenLink url={source.data.url} label="在新窗口打开 PDF" />
      </header>
      {(position.data?.stale || (position.data?.position?.representation_id === representation.id && position.data.position.source_hash !== representation.sha256)) && (
        <p className="inline-warning">
          这份文件与上次阅读指纹不一致或无法核验，未自动恢复旧页码。{position.data?.reason}
        </p>
      )}
      {(positionError || position.isError) && (
        <p className="inline-warning" role="status">
          {position.isError ? "上次阅读位置读取失败，自动保存已暂停：" : "阅读位置未保存，后续保存已暂停："}{positionError || getApiErrorMessage(position.error)}
          <button className="text-link" onClick={async () => {
            const result = await position.refetch();
            if (result.isSuccess) {
              saveBlocked.current = false;
              setPositionError("");
              setSaveRevision((value) => value + 1);
            }
          }}>刷新已保存位置并重试</button>
        </p>
      )}
      {loadError ? (
        <div className="viewer-fallback" role="alert">
          <AlertTriangle size={20} />
          <strong>{loadError}</strong>
          <span>重试只读取当前已选版本；新窗口按钮使用同一安全地址，不代表已验证可读。</span>
          <div className="button-row">
            <button className="button secondary" onClick={() => setLoadRevision((value) => value + 1)}>重试加载 PDF</button>
            <button className="button secondary" onClick={() => void source.refetch().then(() => setLoadRevision((value) => value + 1))}>重试安全会话</button>
          </div>
        </div>
      ) : pdfDocument && positionReady ? (
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
                  if (Number.isInteger(value) && value >= 1 && value <= pdfDocument.numPages)
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
            <label className="pdf-zoom-picker">缩放
              <select aria-label="PDF 缩放" value={zoom} onChange={(event) => setZoom(event.target.value === "fit" ? "fit" : Number(event.target.value))}>
                <option value="fit">适合宽度</option>
                {[0.5, 0.75, 1, 1.25, 1.5, 2].map((value) => <option key={value} value={value}>{value * 100}%</option>)}
              </select>
            </label>
          </div>
          {!representation.sha256 && <p className="inline-warning">当前文件没有可核验的指纹，暂不支持创建或显示页内高亮；原有摘录仍保留。</p>}
          {renderError && (
            <p className="inline-error" role="alert">
              {renderError}
              <button className="text-link" onClick={() => setLoadRevision((value) => value + 1)}>重试加载本页</button>
            </p>
          )}
          {!!stale.length && (
            <p className="inline-warning">
              本页 {stale.length}{" "}
              条高亮的文件指纹不匹配或无法核验，已隐藏其标记。摘录内容仍保留。
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
        <LoadingState label={pdfDocument ? "正在恢复阅读位置…" : "正在解析 PDF…"} />
      )}
    </section>
  );
}
