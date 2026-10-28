import { useMutation, useQuery } from "@tanstack/react-query";
import { FolderOpen, HardDrive, RefreshCw } from "lucide-react";
import { useState } from "react";
import { getApiErrorMessage } from "../api/client";
import { desktopLibrary } from "../api/desktop";
import type { DataDirectorySelection } from "../api/types";

export function LibraryLocationSettings() {
  const [selection, setSelection] = useState<DataDirectorySelection | null>(
    null,
  );
  const location = useQuery({
    queryKey: ["desktop-data-directory"],
    queryFn: () => desktopLibrary.getDataDirectory(),
    retry: false,
  });
  const switchLibrary = useMutation({
    mutationFn: (path: string) => desktopLibrary.switchDataDirectory(path),
  });
  const choose = useMutation({
    mutationFn: () => desktopLibrary.chooseDataDirectory(),
    onSuccess: (choice) => {
      if (choice) {
        setSelection(choice);
        switchLibrary.reset();
      }
    },
  });
  const path = location.data?.path;
  const locked = location.data?.locked_by_environment;
  const switching = switchLibrary.isPending || switchLibrary.isSuccess;

  return (
    <section
      className="section-block library-location-settings"
      aria-labelledby="library-location-title"
    >
      <div className="section-heading">
        <div>
          <p className="eyebrow">本机存储</p>
          <h2 id="library-location-title">
            <HardDrive size={19} aria-hidden="true" />
            资料库位置
          </h2>
        </div>
      </div>
      <p>
        选择资料库所在目录，可将新资料库建在其他磁盘，或打开已经存在的资料库。
      </p>
      {location.isPending ? (
        <p role="status">正在读取资料库位置…</p>
      ) : location.isError ? (
        <div>
          <p className="inline-error" role="alert">
            {getApiErrorMessage(location.error)}
          </p>
          <button
            className="button secondary compact"
            type="button"
            onClick={() => void location.refetch()}
          >
            重新读取位置
          </button>
        </div>
      ) : (
        <dl className="library-directory">
          <dt>当前目录</dt>
          <dd className="mono">{path}</dd>
        </dl>
      )}
      {locked && (
        <p className="inline-warning" role="status">
          当前位置由启动配置 HUB_DATA_DIR
          固定，无法在这里切换。修改启动配置后重启应用即可使用新位置。
        </p>
      )}
      <button
        className="button secondary compact"
        type="button"
        disabled={
          location.isPending ||
          location.isError ||
          locked ||
          choose.isPending ||
          switching
        }
        onClick={() => choose.mutate()}
      >
        <FolderOpen size={16} aria-hidden="true" />
        {choose.isPending ? "正在选择目录…" : "选择资料库目录"}
      </button>
      {choose.isError && (
        <p className="inline-error" role="alert">
          {getApiErrorMessage(choose.error)}
        </p>
      )}
      {selection && (
        <div className="library-location-preview">
          <dl className="library-directory">
            <dt>已选择目录</dt>
            <dd className="mono">{selection.path}</dd>
          </dl>
          <p>
            <strong>
              {selection.is_existing_library
                ? "将在此目录打开已有资料库。"
                : "将在此目录创建空资料库。"}
            </strong>
          </p>
          <p>
            切换需要重启应用。当前资料库仍保存在原位置，此操作不会搬动或合并资料。
          </p>
          <div className="button-row">
            <button
              className="button primary compact"
              type="button"
              disabled={switching || locked}
              onClick={() => switchLibrary.mutate(selection.path)}
            >
              <RefreshCw
                size={15}
                className={switchLibrary.isPending ? "spin" : undefined}
                aria-hidden="true"
              />
              {switching ? "正在切换并重启…" : "切换并重启"}
            </button>
            <button
              className="button secondary compact"
              type="button"
              disabled={switching}
              onClick={() => {
                setSelection(null);
                choose.reset();
                switchLibrary.reset();
              }}
            >
              取消切换
            </button>
          </div>
        </div>
      )}
      {switchLibrary.isError && (
        <p className="inline-error" role="alert">
          切换未完成：{getApiErrorMessage(switchLibrary.error)}
        </p>
      )}
      {switchLibrary.isSuccess && (
        <p role="status">已提交切换，正在重启应用…</p>
      )}
    </section>
  );
}
