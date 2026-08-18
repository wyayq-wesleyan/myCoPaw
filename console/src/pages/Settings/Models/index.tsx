import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Button, Input, Modal } from "@agentscope-ai/design";
import { PlusOutlined, SearchOutlined, SyncOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import type { ProviderInfo } from "../../../api/types/provider";
import { PageHeader } from "@/components/PageHeader";
import { useProviders } from "./useProviders";
import {
  CustomProviderModal,
  LoadingState,
  ModelManageModal,
  ModelsSection,
  ProviderCard,
  ProviderConfigModal,
} from "./components";
import styles from "./index.module.less";

/**
 * The offline edition intentionally has one provider surface: administrator
 * configured OpenAI-compatible services on the intranet. The official
 * provider catalogue is neither rendered nor registered by the backend.
 */
function ModelsPage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const { providers, activeModels, loading, error, fetchAll } = useProviders();
  const [addProviderOpen, setAddProviderOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [configModalProvider, setConfigModalProvider] =
    useState<ProviderInfo | null>(null);
  const [modelsModalProvider, setModelsModalProvider] =
    useState<ProviderInfo | null>(null);
  const [llmModalOpen, setLlmModalOpen] = useState(false);

  useEffect(() => {
    const providerId = searchParams.get("provider");
    if (!providerId || providers.length === 0) return;
    const provider = providers.find((item) => item.id === providerId);
    if (provider) {
      setConfigModalProvider(provider);
      setSearchParams({}, { replace: true });
    }
  }, [providers, searchParams, setSearchParams]);

  useEffect(() => {
    if (!configModalProvider) return;
    const current = providers.find((item) => item.id === configModalProvider.id);
    if (current && current !== configModalProvider) setConfigModalProvider(current);
  }, [providers, configModalProvider]);

  useEffect(() => {
    if (!modelsModalProvider) return;
    const current = providers.find((item) => item.id === modelsModalProvider.id);
    if (current && current !== modelsModalProvider) setModelsModalProvider(current);
  }, [providers, modelsModalProvider]);

  const configuredProviders = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    return providers
      .filter((provider) => provider.is_custom)
      .filter(
        (provider) =>
          !query ||
          provider.name.toLowerCase().includes(query) ||
          provider.id.toLowerCase().includes(query),
      )
      .sort((left, right) => {
        const leftModels = left.models.length + left.extra_models.length;
        const rightModels = right.models.length + right.extra_models.length;
        return rightModels - leftModels || left.name.localeCompare(right.name);
      });
  }, [providers, searchQuery]);

  const refreshProvidersSilently = useCallback(() => fetchAll(false), [fetchAll]);

  const renderProviderCards = (items: ProviderInfo[]) =>
    items.map((provider) => (
      <ProviderCard
        key={provider.id}
        provider={provider}
        activeModels={activeModels}
        onSaved={refreshProvidersSilently}
        onOpenConfig={setConfigModalProvider}
        onOpenModels={setModelsModalProvider}
      />
    ));

  return (
    <div className={styles.settingsPage}>
      {loading ? (
        <LoadingState message={t("models.loading")} />
      ) : error ? (
        <LoadingState message={error} error onRetry={fetchAll} />
      ) : (
        <>
          <PageHeader parent={t("nav.settings")} current="模型服务" />
          <div className={styles.content}>
            <div className={styles.providersBlock}>
              <div className={styles.sectionHeaderRow}>
                <PageHeader
                  current="内网 OpenAI 兼容服务"
                  className={styles.providersPageHeader}
                />
                <div className={styles.headerRight}>
                  <div
                    className={[
                      styles.llmPill,
                      activeModels?.active_llm
                        ? styles.llmPillOn
                        : styles.llmPillOff,
                    ].join(" ")}
                    onClick={() => setLlmModalOpen(true)}
                    role="button"
                    tabIndex={0}
                  >
                    <span
                      className={
                        activeModels?.active_llm
                          ? styles.llmPillDot
                          : styles.llmPillDotOff
                      }
                    />
                    <span className={styles.llmPillLabel}>默认对话模型:</span>
                    <span className={styles.llmPillValue}>
                      {activeModels?.active_llm?.provider_id || "未设置"} /{" "}
                      {activeModels?.active_llm?.model || "未设置"}
                    </span>
                    <span className={styles.llmPillEdit}>设置</span>
                  </div>
                  <div className={styles.searchRow}>
                    <Input
                      placeholder="搜索已配置服务"
                      value={searchQuery}
                      onChange={(event) => setSearchQuery(event.target.value)}
                      className={styles.searchInput}
                      prefix={<SearchOutlined />}
                      allowClear
                      autoComplete="off"
                    />
                    <Button
                      icon={<SyncOutlined />}
                      onClick={() => fetchAll()}
                      className={styles.searchBtn}
                      title="刷新"
                    />
                  </div>
                  <Button
                    type="primary"
                    icon={<PlusOutlined />}
                    onClick={() => setAddProviderOpen(true)}
                    className={styles.addProviderBtn}
                  >
                    添加服务
                  </Button>
                </div>
              </div>

              <div className={styles.tabsNav}>
                <div className={`${styles.tabItem} ${styles.tabItemActive}`}>
                  已配置服务 ({configuredProviders.length})
                </div>
              </div>

              <div className={styles.panelSection}>
                <div className={styles.panelTitle}>
                  <span className={styles.panelDotGreen} />
                  内网模型服务
                </div>
                {configuredProviders.length > 0 ? (
                  <div className={styles.providerCards}>
                    {renderProviderCards(configuredProviders)}
                  </div>
                ) : (
                  <div className={styles.emptyConfigured}>
                    <p>暂未配置模型服务</p>
                    <Button type="primary" onClick={() => setAddProviderOpen(true)}>
                      添加内网 OpenAI 服务
                    </Button>
                  </div>
                )}
              </div>
            </div>

            <CustomProviderModal
              open={addProviderOpen}
              onClose={() => setAddProviderOpen(false)}
              onSaved={fetchAll}
            />

            <Modal
              open={llmModalOpen}
              title="默认对话模型"
              footer={null}
              onCancel={() => setLlmModalOpen(false)}
              destroyOnClose
              width={520}
            >
              <ModelsSection
                providers={configuredProviders}
                activeModels={activeModels}
                onSaved={() => {
                  fetchAll();
                  setLlmModalOpen(false);
                }}
              />
            </Modal>

            {configModalProvider && (
              <ProviderConfigModal
                provider={configModalProvider}
                activeModels={activeModels}
                open
                onClose={() => setConfigModalProvider(null)}
                onSaved={refreshProvidersSilently}
              />
            )}
            {modelsModalProvider && (
              <ModelManageModal
                provider={modelsModalProvider}
                open
                onClose={() => setModelsModalProvider(null)}
                onSaved={refreshProvidersSilently}
                onProviderUpdated={setModelsModalProvider}
              />
            )}
          </div>
        </>
      )}
    </div>
  );
}

export default ModelsPage;
