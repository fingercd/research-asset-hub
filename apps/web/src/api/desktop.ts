import { invoke, isTauri } from "@tauri-apps/api/core";
import type { DataDirectoryInfo, DataDirectorySelection } from "./types";

async function invokeDirectoryCommand<T>(
  command: string,
  args?: { path: string },
): Promise<T> {
  try {
    return await invoke<T>(command, args);
  } catch (error) {
    // Rust Result<_, String> rejects with text; preserve that message in the UI.
    throw typeof error === "string" ? new Error(error) : error;
  }
}

export const desktopLibrary = {
  isDesktop: isTauri,
  getDataDirectory: () =>
    invokeDirectoryCommand<DataDirectoryInfo>("get_data_directory"),
  chooseDataDirectory: () =>
    invokeDirectoryCommand<DataDirectorySelection | null>(
      "choose_data_directory",
    ),
  switchDataDirectory: (path: string) =>
    invokeDirectoryCommand<void>("switch_data_directory", { path }),
};
