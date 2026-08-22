import { useState, useEffect, useCallback, useRef } from "react";
import { Dropdown, Spin, Tooltip } from "antd";
import { useAppMessage } from "../../../hooks/useAppMessage";
import {
  CheckOutlined,
  LoadingOutlined,
  SearchOutlined,
  CloseCircleFilled,
  DownOutlined,
  UpOutlined,
} from "@ant-design/icons";
import { useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { providerApi } from "../../../api/modules/provider";
import type { ProviderInfo, ActiveModelsInfo } from "../../../api/types";
import { useAgentStore } from "../../../stores/agentStore";
import { ProviderIcon } from "../../Settings/Models/components/ProviderIconComponent";
import { useTurnUsageStore } from "../turnUsageStore";
import styles from "./index.module.less";

/** Sync Chat context ring with the active model's effective window. */
function publishActiveMaxInputLength(
  effectiveMaxInputLength: number | null | undefined,
): void {
  const maxInputLength =
    typeof effectiveMaxInputLength === "number"
      ? effectiveMaxInputLength
      : null;
  useTurnUsageStore.getState().setActiveMaxInputLength(maxInputLength);
  if (typeof maxInputLength === "number" && maxInputLength > 0) {
    window.dispatchEvent(
      new CustomEvent("model-switched", {
        detail: { maxInputLength },
      }),
    );
  }
}

interface EligibleProvider {
  id: string;
  name: string;
  models: ProviderInfo["models"];
}

export default function ModelSelector() {
  const { t } = useTranslation();
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [activeModels, setActiveModels] = useState<ActiveModelsInfo | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [collapsedProviders, setCollapsedProviders] = useState<Set<string>>(
    () => {
      try {
        const raw = localStorage.getItem("qwenpaw_model_selector_collapsed");
        return raw ? new Set(JSON.parse(raw) as string[]) : new Set();
      } catch {
        return new Set();
      }
    },
  );
  const savingRef = useRef(false);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const location = useLocation();
  const { selectedAgent } = useAgentStore();
  const { message } = useAppMessage();

  const [expandedModels, setExpandedModels] = useState<Record<string, number>>(
    {},
  );

  // Mobile viewport detection for dropdown placement
  const [isMobile, setIsMobile] = useState(() => window.innerWidth <= 768);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 768px)");
    const handler = (e: MediaQueryListEvent | MediaQueryList) => {
      setIsMobile(e.matches);
    };
    handler(media);
    media.addEventListener("change", handler);
    return () => media.removeEventListener("change", handler);
  }, []);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const [provData, activeData] = await Promise.all([
        providerApi.listProviders(),
        providerApi.getActiveModels({
          scope: "effective",
          agent_id: selectedAgent,
        }),
      ]);
      if (Array.isArray(provData)) setProviders(provData);
      if (activeData) {
        setActiveModels(activeData);
        publishActiveMaxInputLength(activeData.effective_max_input_length);
      }
    } catch (err) {
      console.error("ModelSelector: failed to load data", err);
    } finally {
      setLoading(false);
    }
  }, [selectedAgent]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // Re-sync active model whenever the route switches back to /chat
  const prevPathRef = useRef(location.pathname);
  useEffect(() => {
    const prev = prevPathRef.current;
    const curr = location.pathname;
    prevPathRef.current = curr;
    const comingToChat = curr.startsWith("/chat") && !prev.startsWith("/chat");
    if (comingToChat) {
      providerApi
        .getActiveModels({
          scope: "effective",
          agent_id: selectedAgent,
        })
        .then((activeData) => {
          if (activeData) {
            setActiveModels(activeData);
            publishActiveMaxInputLength(activeData.effective_max_input_length);
          }
        })
        .catch(() => {});
    }
  }, [location.pathname, selectedAgent]);

  // The API returns the effective view for the current account.  Do not apply
  // client-side provider/key/free-tier rules here: those rules used to expose
  // built-in and online models to ordinary users.  Every model that reaches
  // this component has already passed the administrator entitlement check.
  const eligibleProviders: EligibleProvider[] = providers
    .map((p) => ({
      id: p.id,
      name: p.name,
        models: [...(p.models ?? []), ...(p.extra_models ?? [])].filter(
          (model) => (model.model_type ?? "chat") === "chat",
        ),
    }))
    .filter((p) => p.models.length > 0);

  // Filter by search query
  const trimmedSearch = searchQuery.trim();
  const filterProviders = (list: EligibleProvider[]) => {
    if (!trimmedSearch) return list;
    const query = trimmedSearch.toLowerCase();
    return list
      .map((p) => ({
        ...p,
        models: p.models.filter(
          (m) =>
            (m.name || m.id).toLowerCase().includes(query) ||
            p.name.toLowerCase().includes(query),
        ),
      }))
      .filter(
        (p) => p.models.length > 0 || p.name.toLowerCase().includes(query),
      );
  };

  const filteredProviders = filterProviders(eligibleProviders);

  // Focus search input when dropdown opens; clear query when closes
  useEffect(() => {
    if (open) {
      setTimeout(() => searchInputRef.current?.focus(), 50);
    } else {
      setSearchQuery("");
    }
  }, [open]);

  const activeProviderId = activeModels?.active_llm?.provider_id;
  const activeModelId = activeModels?.active_llm?.model;

  // Display label for trigger button
  const activeModelName = (() => {
    if (!activeProviderId || !activeModelId)
      return t("modelSelector.selectModel");
    for (const p of eligibleProviders) {
      if (p.id === activeProviderId) {
        const m = p.models.find((m) => m.id === activeModelId);
        if (m) return `${p.name} - ${m.name || m.id}`;
      }
    }
    return t("modelSelector.selectModel");
  })();

  const showActiveProviderIcon = eligibleProviders.some(
    (provider) =>
      provider.id === activeProviderId &&
      provider.models.some((model) => model.id === activeModelId),
  );

  // Marquee the trigger name on very narrow screens when it overflows.
  const triggerNameRef = useRef<HTMLSpanElement | null>(null);
  const triggerNameMeasureRef = useRef<HTMLSpanElement | null>(null);
  const [shouldMarquee, setShouldMarquee] = useState(false);

  useEffect(() => {
    const check = () => {
      const w = typeof window !== "undefined" ? window.innerWidth : 0;
      if (w > 480) {
        setShouldMarquee(false);
        return;
      }
      const containerWidth =
        triggerNameRef.current?.getBoundingClientRect().width ?? 0;
      const textWidth =
        triggerNameMeasureRef.current?.getBoundingClientRect().width ?? 0;
      // Small tolerance to avoid borderline jitter.
      setShouldMarquee(textWidth > containerWidth + 2);
    };

    check();
    window.addEventListener("resize", check);
    return () => window.removeEventListener("resize", check);
  }, [activeModelName]);

  const handleOpenChange = useCallback(
    async (next: boolean) => {
      setOpen(next);
      if (next) {
        try {
          const activeData = await providerApi.getActiveModels({
            scope: "effective",
            agent_id: selectedAgent,
          });
          if (activeData) setActiveModels(activeData);
        } catch {
          // ignore
        }
      }
    },
    [selectedAgent],
  );

  const handleSelect = async (providerId: string, modelId: string) => {
    if (savingRef.current) return;
    if (providerId === activeProviderId && modelId === activeModelId) {
      setOpen(false);
      return;
    }

    setOpen(false);

    savingRef.current = true;
    setSaving(true);
    try {
      const updated = await providerApi.setActiveLlm({
        provider_id: providerId,
        model: modelId,
        scope: "agent",
        agent_id: selectedAgent,
      });
      setActiveModels(
        updated?.active_llm
          ? updated
          : {
              ...updated,
              active_llm: { provider_id: providerId, model: modelId },
            },
      );
      publishActiveMaxInputLength(updated?.effective_max_input_length);
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : t("modelSelector.switchFailed");
      message.error(msg);
    } finally {
      setSaving(false);
      savingRef.current = false;
    }
  };


  const toggleProviderCollapse = (providerId: string) => {
    setCollapsedProviders((prev) => {
      const next = new Set(prev);
      if (next.has(providerId)) {
        next.delete(providerId);
      } else {
        next.add(providerId);
      }
      localStorage.setItem(
        "qwenpaw_model_selector_collapsed",
        JSON.stringify([...next]),
      );
      return next;
    });
  };

  const renderProviderModels = (provider: EligibleProvider) => {
    const isCollapsed = collapsedProviders.has(provider.id);
    const visibleCount = expandedModels[provider.id] ?? 5;
    const visibleModels = provider.models.slice(0, visibleCount);
    const remaining = provider.models.length - visibleCount;
    const hasMore = remaining > 0;

    return (
      <div key={provider.id} className={styles.providerGroup}>
        <div
          className={styles.providerHeader}
          onClick={() => toggleProviderCollapse(provider.id)}
        >
          <ProviderIcon providerId={provider.id} size={16} />
          <span className={styles.providerHeaderName}>{provider.name}</span>
          <span className={styles.collapseIcon}>
            {isCollapsed ? <DownOutlined /> : <UpOutlined />}
          </span>
        </div>
        {!isCollapsed && (
          <>
            {visibleModels.map((model) => {
              const isActive =
                provider.id === activeProviderId && model.id === activeModelId;
              return (
                <div
                  key={model.id}
                  className={[
                    styles.modelItem,
                    isActive ? styles.modelItemActive : "",
                  ].join(" ")}
                  onClick={() => handleSelect(provider.id, model.id)}
                >
                  <span className={styles.modelName}>
                    {model.name || model.id}
                  </span>
                  <div className={styles.modelTags}>
                    {(model.supports_image || model.supports_multimodal) && (
                      <span className={styles.visionTag}>
                        {t("modelSelector.vision")}
                      </span>
                    )}
                    {isActive && <CheckOutlined className={styles.checkIcon} />}
                  </div>
                </div>
              );
            })}
            {hasMore && (
              <div
                className={styles.viewMore}
                onClick={(e) => {
                  e.stopPropagation();
                  setExpandedModels((prev) => ({
                    ...prev,
                    [provider.id]: visibleCount + 10,
                  }));
                }}
              >
                {t("modelSelector.viewMore", {
                  count: Math.min(10, remaining),
                })}
              </div>
            )}
          </>
        )}
      </div>
    );
  };

  const dropdownContent = (
    <div className={styles.panel}>
      <div className={styles.searchWrapper}>
        <SearchOutlined className={styles.searchIcon} />
        <input
          ref={searchInputRef}
          className={styles.searchInput}
          placeholder={t("modelSelector.searchModels")}
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
        />
        {searchQuery && (
          <CloseCircleFilled
            className={styles.searchClear}
            onClick={(e) => {
              e.stopPropagation();
              setSearchQuery("");
              searchInputRef.current?.focus();
            }}
          />
        )}
      </div>

      <div className={styles.listContainer}>
        {loading ? (
          <div className={styles.spinWrapper}>
            <Spin size="small" />
          </div>
        ) : filteredProviders.length > 0 ? (
          filteredProviders.map(renderProviderModels)
        ) : (
          <div className={styles.emptyTip}>
            {trimmedSearch
              ? t("modelSelector.noModelsFound")
              : t("modelSelector.noAuthorizedModels")}
          </div>
        )}
      </div>
    </div>
  );

  return (
    <>
      <Dropdown
        open={open}
        onOpenChange={handleOpenChange}
        popupRender={() => (
          <div style={{ transform: "translateY(0)" }}>{dropdownContent}</div>
        )}
        trigger={["click"]}
        placement={isMobile ? "bottomCenter" : "bottomLeft"}
      >
        <Tooltip title={t("chat.modelSelectTooltip")} mouseEnterDelay={0.5}>
          <div
            className={[styles.trigger, open ? styles.triggerActive : ""].join(
              " ",
            )}
          >
            {saving && (
              <LoadingOutlined style={{ fontSize: 11, color: "#2A63AD" }} />
            )}
            {showActiveProviderIcon && activeProviderId && (
              <ProviderIcon providerId={activeProviderId} size={16} />
            )}
            <span className={styles.triggerName} ref={triggerNameRef}>
              {shouldMarquee ? (
                <span className={styles.marquee}>{activeModelName}</span>
              ) : (
                activeModelName
              )}
            </span>
            {/* Hidden span used to measure intrinsic text width. Placed
                outside .triggerName so it does not duplicate text for
                screen readers or testing-library queries. */}
            <span
              ref={triggerNameMeasureRef}
              aria-hidden="true"
              style={{
                position: "absolute",
                visibility: "hidden",
                whiteSpace: "nowrap",
                pointerEvents: "none",
              }}
            >
              {activeModelName}
            </span>
          </div>
        </Tooltip>
      </Dropdown>

    </>
  );
}
