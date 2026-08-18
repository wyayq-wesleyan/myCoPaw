import { Suspense, useEffect, useMemo, useState } from "react";
import { Layout, Spin } from "antd";
import {
  Routes,
  Route,
  Navigate,
  useLocation,
  matchPath,
} from "react-router-dom";
import { useTranslation } from "react-i18next";
import Sidebar from "../Sidebar";
import Header from "../Header";
import ConsolePollService from "../../components/ConsolePollService";
import { AgentStatusPollingController } from "../../components/AgentStatusPollingController";
import { ChunkErrorBoundary } from "../../components/ChunkErrorBoundary";
import { useSyncCodingMode } from "../../stores/useSyncCodingMode";
import styles from "../index.module.less";
import { useRoutes } from "../../plugins/registry/hooks";
import { Slot } from "../../plugins/registry/Slot";
import { authApi } from "../../api/modules/auth";

const { Content } = Layout;

const ALWAYS_ALLOWED_ROUTE_IDS = new Set([
  "core.root",
  "core.chat",
  "core.acp-alias",
]);
const OFFLINE_DISABLED_ROUTE_IDS = new Set(["core.channels"]);

/**
 * Find the registered route whose path pattern matches the current URL.
 * Falls back to "core.chat" so the sidebar always has a sensible
 * highlight, mirroring the old `pathToKey` default.
 */
function pickSelectedKey(
  currentPath: string,
  routes: ReturnType<typeof useRoutes>,
): string {
  for (const r of routes) {
    if (matchPath({ path: r.path, end: r.path === "/" }, currentPath)) {
      return r.id;
    }
  }
  return "core.chat";
}

export default function MainLayout() {
  const { t } = useTranslation();
  const location = useLocation();
  const currentPath = location.pathname;
  const routes = useRoutes();
  const [access, setAccess] = useState<{
    loading: boolean;
    isAdmin: boolean;
    permissions: Set<string>;
  }>({ loading: true, isAdmin: false, permissions: new Set() });

  // Backend is the source of truth for Coding Mode state — refill the
  // in-memory store every time the selected agent changes.
  useSyncCodingMode();

  const selectedKey = useMemo(
    () => pickSelectedKey(currentPath, routes),
    [currentPath, routes],
  );

  useEffect(() => {
    let cancelled = false;
    const loadAccess = async () => {
      try {
        const status = await authApi.getStatus();
        if (!status.enabled) {
          if (!cancelled) {
            setAccess({ loading: false, isAdmin: true, permissions: new Set() });
          }
          return;
        }
        const user = await authApi.getCurrentUser();
        if (!cancelled) {
          setAccess({
            loading: false,
            isAdmin: user.role === "admin",
            permissions: new Set(user.menu_permissions ?? []),
          });
        }
      } catch {
        // AuthGuard will handle an expired credential. Keeping the page locked
        // while that happens prevents a direct URL from flashing protected UI.
        if (!cancelled) {
          setAccess({ loading: false, isAdmin: false, permissions: new Set() });
        }
      }
    };
    void loadAccess();
    return () => {
      cancelled = true;
    };
  }, []);

  // PawApp inline routes (`/apps/<id>`) are rendered *inside* the App Center
  // page (with its "← App Center" bar), never as standalone full-page routes.
  // They stay in the registry so the App Center can look up their component;
  // we just skip them here. The App Center's own `/apps/:appId` route (with a
  // colon) is kept, so a deep-link / refresh lands on the App Center wrapper.
  const renderableRoutes = useMemo(
    () => routes.filter((r) => !/^\/apps\/(?!:)/.test(r.path)),
    [routes],
  );

  const canOpenCurrentRoute =
    !OFFLINE_DISABLED_ROUTE_IDS.has(selectedKey) &&
    (access.isAdmin ||
      ALWAYS_ALLOWED_ROUTE_IDS.has(selectedKey) ||
      access.permissions.has(selectedKey));

  return (
    <Layout className={styles.mainLayout}>
      <Header />
      <Layout>
        <Sidebar selectedKey={selectedKey} />
        <Content className="page-container">
          <ConsolePollService />
          <AgentStatusPollingController />
          <Slot name="content.statusBar" kind="fill" />
          <div className="page-content">
            <ChunkErrorBoundary resetKey={currentPath}>
              <Suspense
                fallback={
                  <Spin
                    tip={t("common.loading")}
                    style={{ display: "block", margin: "20vh auto" }}
                  />
                }
              >
                {access.loading ? (
                  <Spin
                    tip={t("common.loading")}
                    style={{ display: "block", margin: "20vh auto" }}
                  />
                ) : canOpenCurrentRoute ? (
                  <Routes>
                    {renderableRoutes.map((r) => (
                      <Route key={r.id} path={r.path} element={<r.Component />} />
                    ))}
                  </Routes>
                ) : (
                  <Navigate to="/chat" replace />
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
