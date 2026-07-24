import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { AnnotationsPage } from "./pages/AnnotationsPage";
import { ChatPage } from "./pages/ChatPage";
import { ConflictsPage } from "./pages/ConflictsPage";
import { DomainDetailPage, DomainsPage } from "./pages/DomainsPage";
import { HomePage } from "./pages/HomePage";
import { JobsPage } from "./pages/JobsPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { SearchPage } from "./pages/SearchPage";
import { SettingsPage } from "./pages/SettingsPage";

const AssetDetailPage = lazy(() =>
  import("./pages/AssetDetailPage").then((module) => ({
    default: module.AssetDetailPage,
  })),
);
const ComparePage = lazy(() =>
  import("./pages/ComparePage").then((module) => ({
    default: module.ComparePage,
  })),
);
const GraphPage = lazy(() =>
  import("./pages/GraphPage").then((module) => ({
    default: module.GraphPage,
  })),
);

export default function App() {
  return (
    <Suspense
      fallback={
        <div className="route-loading" role="status">
          正在加载页面…
        </div>
      }
    >
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<HomePage />} />
          <Route path="search" element={<SearchPage />} />
          <Route path="assets/:assetId" element={<AssetDetailPage />} />
          <Route path="domains" element={<DomainsPage />} />
          <Route path="domains/:slug" element={<DomainDetailPage />} />
          <Route path="graph" element={<GraphPage />} />
          <Route path="compare" element={<ComparePage />} />
          <Route path="annotations" element={<AnnotationsPage />} />
          <Route path="chat" element={<ChatPage />} />
          <Route path="jobs" element={<JobsPage />} />
          <Route path="conflicts" element={<ConflictsPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="404" element={<NotFoundPage />} />
          <Route path="*" element={<Navigate to="/404" replace />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
