import { Suspense, useEffect, useMemo } from "react";
import { Layout, Spin } from "antd";
import { Routes, Route, useLocation, Navigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import Sidebar from "../Sidebar";
import Header from "../Header";
import ConsolePollService from "../../components/ConsolePollService";
import { AgentStatusPollingController } from "../../components/AgentStatusPollingController";
import { ChunkErrorBoundary } from "../../components/ChunkErrorBoundary";
import { useSyncCodingMode } from "../../stores/useSyncCodingMode";
import {
  useDeniedRouteIds,
  useHubPermissionsStore,
} from "../../stores/hubPermissionsStore";
import styles from "../index.module.less";
import { useRoutes } from "../../plugins/registry/hooks";
import { Slot } from "../../plugins/registry/Slot";
import { pickSelectedKey } from "./routeSelection";
import { deniedPathsForRoutes, isPathDenied } from "../registry/permissions";

const { Content } = Layout;

export default function MainLayout({ hubMode = false }: { hubMode?: boolean }) {
  const { t } = useTranslation();
  const location = useLocation();
  const currentPath = location.pathname;
  const routes = useRoutes();
  const loadPermissions = useHubPermissionsStore((state) => state.load);
  const deniedRouteIds = useDeniedRouteIds();

  // Hub deployments fetch the role deny-list once after auth settles.
  useEffect(() => {
    if (hubMode) void loadPermissions();
  }, [hubMode, loadPermissions]);

  // Route-level guard: deep links to denied pages bounce to chat. The
  // sidebar filter hides the entry points; this closes direct URLs. UX
  // only — the hub proxy ACL still rejects the underlying APIs.
  const routeIdToPath = useMemo(
    () => new Map(routes.map((route) => [route.id, route.path])),
    [routes],
  );
  const deniedPaths = useMemo(
    () => deniedPathsForRoutes(routeIdToPath, deniedRouteIds),
    [deniedRouteIds, routeIdToPath],
  );
  const pathDenied = isPathDenied(currentPath, deniedPaths);

  // Backend is the source of truth for Coding Mode state — refill the
  // in-memory store every time the selected agent changes.
  useSyncCodingMode();

  const selectedKey = useMemo(
    () => pickSelectedKey(currentPath, routes),
    [currentPath, routes],
  );
  const settingsCenterActive = selectedKey === "core.settings-center";

  // PawApp inline routes (`/apps/<id>`) are rendered *inside* the App Center
  // page (with its "← App Center" bar), never as standalone full-page routes.
  // They stay in the registry so the App Center can look up their component;
  // we just skip them here. The App Center's own `/apps/:appId` route (with a
  // colon) is kept, so a deep-link / refresh lands on the App Center wrapper.
  const renderableRoutes = useMemo(
    () => routes.filter((r) => !/^\/apps\/(?!:)/.test(r.path)),
    [routes],
  );

  return (
    <Layout className={styles.mainLayout}>
      {!settingsCenterActive && (
        <Sidebar selectedKey={selectedKey} hubMode={hubMode} />
      )}
      <Layout className={styles.mainContentLayout}>
        <Header showBrand={settingsCenterActive} />
        <Content className="page-container">
          <ConsolePollService />
          <AgentStatusPollingController />
          <Slot name="content.statusBar" kind="fill" />
          <div className="page-content">
            <ChunkErrorBoundary
              resetKey={currentPath}
              canRestartRuntime={hubMode}
            >
              <Suspense
                fallback={
                  <Spin
                    tip={t("common.loading")}
                    style={{ display: "block", margin: "20vh auto" }}
                  />
                }
              >
                {pathDenied ? (
                  <Navigate to="/chat" replace />
                ) : (
                  <Routes>
                    {renderableRoutes.map((r) => (
                      <Route
                        key={r.id}
                        path={r.path}
                        element={<r.Component />}
                      />
                    ))}
                  </Routes>
                )}
              </Suspense>
            </ChunkErrorBoundary>
          </div>
        </Content>
      </Layout>
      <Slot name="overlay.global" kind="fill" />
    </Layout>
  );
}
