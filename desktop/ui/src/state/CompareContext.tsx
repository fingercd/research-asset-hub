import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { Asset } from "../api/types";

const STORAGE_KEY = "rah.compare-assets";

interface CompareContextValue {
  assets: Asset[];
  add: (asset: Asset) => boolean;
  remove: (id: string) => void;
  toggle: (asset: Asset) => boolean;
  clear: () => void;
  has: (id: string) => boolean;
}

const CompareContext = createContext<CompareContextValue | null>(null);

function isPaperAsset(asset: Asset) {
  const assetType = asset.asset_type ?? asset.type;
  return typeof assetType === "string" && assetType.trim().toLowerCase() === "paper";
}

function sanitizeAssets(assets: Asset[]) {
  const seen = new Set<string>();
  return assets
    .filter((asset) => {
      if (!isPaperAsset(asset) || seen.has(asset.id)) return false;
      seen.add(asset.id);
      return true;
    })
    .slice(0, 4);
}

function readInitialAssets(): Asset[] {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "[]");
    return Array.isArray(parsed) ? sanitizeAssets(parsed) : [];
  } catch {
    return [];
  }
}

export function CompareProvider({ children }: { children: ReactNode }) {
  const [assets, setAssets] = useState<Asset[]>(readInitialAssets);

  const update = useCallback((next: Asset[]) => {
    const safe = sanitizeAssets(next);
    setAssets(safe);
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(safe));
    } catch {
      // The basket still works for this session when storage is unavailable.
    }
  }, []);

  const add = useCallback(
    (asset: Asset) => {
      if (!isPaperAsset(asset)) return false;
      if (assets.some((item) => item.id === asset.id)) return true;
      if (assets.length >= 4) return false;
      update([...assets, asset]);
      return true;
    },
    [assets, update],
  );

  const remove = useCallback(
    (id: string) => update(assets.filter((asset) => asset.id !== id)),
    [assets, update],
  );

  const toggle = useCallback(
    (asset: Asset) => {
      if (!isPaperAsset(asset)) return false;
      if (assets.some((item) => item.id === asset.id)) {
        remove(asset.id);
        return false;
      }
      return add(asset);
    },
    [add, assets, remove],
  );

  const value = useMemo(
    () => ({
      assets,
      add,
      remove,
      toggle,
      clear: () => update([]),
      has: (id: string) => assets.some((asset) => asset.id === id),
    }),
    [add, assets, remove, toggle, update],
  );

  return <CompareContext.Provider value={value}>{children}</CompareContext.Provider>;
}

// The provider and its tightly coupled hook intentionally live together.
// eslint-disable-next-line react-refresh/only-export-components
export function useCompare() {
  const value = useContext(CompareContext);
  if (!value) throw new Error("useCompare 必须在 CompareProvider 内使用");
  return value;
}
