import { useEffect, useState, useMemo } from "react";
import { Modal, Form, Input, Select, Space, Spin } from "antd";
import { useTranslation } from "react-i18next";
import type { AgentSummary } from "@/api/types/agents";
import type { ProviderInfo, ActiveModelsInfo } from "@/api/types/provider";
import { getAgentDisplayName } from "@/utils/agentDisplayName";
import { providerApi } from "@/api/modules/provider";
import { authApi } from "@/api/modules/auth";
import { ProviderIcon } from "../../Models/components/ProviderIconComponent";
import { AgentBackendFields } from "./AgentBackendFields";

interface EligibleProvider {
  id: string;
  name: string;
  models: Array<{ id: string; name: string }>;
}

interface AgentModalProps {
  open: boolean;
  editingAgent: AgentSummary | null;
  form: ReturnType<typeof Form.useForm>[0];
  onSave: () => Promise<void>;
  onCancel: () => void;
}

export function AgentModal({
  open,
  editingAgent,
  form,
  onSave,
  onCancel,
}: AgentModalProps) {
  const { t } = useTranslation();
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [loadingProviders, setLoadingProviders] = useState(false);
  const [isAdmin, setIsAdmin] = useState(false);
  const [globalActiveModel, setGlobalActiveModel] = useState<ActiveModelsInfo | null>(null);

  const selectedProviderId = Form.useWatch("active_model_provider", form);
  const selectedModelId = Form.useWatch("active_model_model", form);
  const selectedBackend = Form.useWatch("backend", form) ?? "qwenpaw";

  useEffect(() => {
    authApi
      .getCurrentUser()
      .then((user) => setIsAdmin(user.role === "admin"))
      .catch(() => {});
  }, []);

  const eligibleProviders: EligibleProvider[] = useMemo(() => {
    return providers
      .filter((p) => {
        const hasModels =
          (p.models?.length ?? 0) + (p.extra_models?.length ?? 0) > 0;
        if (!hasModels) return false;
        // Credentials are only visible to an administrator (the API strips them
        // for everyone else), so this "is the provider configured?" check can only
        // be applied by an admin. For an ordinary account it would reject every
        // built-in provider — whose `api_key` is always empty here — and hide
        // models the administrator explicitly granted, making it impossible to
        // choose a permitted model. Their provider list is already narrowed
        // server-side to entries with at least one authorized model, so "has
        // models" is the whole test that is both meaningful and safe.
        if (!isAdmin) return true;
        if (p.require_api_key === false) return !!p.base_url;
        if (p.is_custom) return !!p.base_url;
        if (p.require_api_key ?? true) return !!p.api_key;
        return true;
      })
      .map((p) => ({
        id: p.id,
        name: p.name,
        models: [...(p.models ?? []), ...(p.extra_models ?? [])].filter(
          (m) => (m.model_type ?? "chat") === "chat",
        ),
      }))
      .filter((p) => p.models.length > 0);
  }, [providers, isAdmin]);

  const availableModels = useMemo(() => {
    if (!selectedProviderId) return [];
    const provider = eligibleProviders.find((p) => p.id === selectedProviderId);
    return provider?.models ?? [];
  }, [selectedProviderId, eligibleProviders]);

  useEffect(() => {
    if (!open || selectedBackend !== "qwenpaw") return;

    setLoadingProviders(true);
    Promise.all([
      providerApi.listProviders(),
      providerApi.getActiveModels({ scope: "global" }),
    ])
      .then(([provData, activeData]) => {
        if (Array.isArray(provData)) setProviders(provData);
        if (activeData) setGlobalActiveModel(activeData);
        // For new agents, pre-select the global default model
        if (!editingAgent && activeData?.active_llm) {
          form.setFieldsValue({
            active_model_provider: activeData.active_llm.provider_id,
            active_model_model: activeData.active_llm.model,
          });
        }
      })
      .catch((err) => console.error("Failed to load providers:", err))
      .finally(() => setLoadingProviders(false));

  }, [
    open,
    selectedBackend,
    editingAgent,
  ]);

  const handleProviderChange = (providerId: string) => {
    form.setFieldsValue({
      active_model_provider: providerId,
      active_model_model: undefined,
    });
  };

  const handleClearModel = () => {
    form.setFieldsValue({
      active_model_provider: undefined,
      active_model_model: undefined,
    });
  };

  return (
    <Modal
      title={
        editingAgent
          ? t("agent.editTitle", {
              name: getAgentDisplayName(editingAgent, t),
            })
          : t("agent.createTitle")
      }
      open={open}
      onOk={onSave}
      onCancel={onCancel}
      width={760}
      styles={{ body: { maxHeight: "72vh", overflowY: "auto" } }}
      okText={t("common.save")}
      cancelText={t("common.cancel")}
    >
      <Form form={form} layout="vertical" autoComplete="off">
        <Form.Item name="active_model_provider" hidden>
          <Input />
        </Form.Item>
        <Form.Item name="active_model_model" hidden>
          <Input />
        </Form.Item>

        {editingAgent ? (
          <AgentBackendFields form={form} open={open} />
        ) : (
          <Form.Item name="backend" hidden>
            <Input />
          </Form.Item>
        )}

        {editingAgent && (
          <Form.Item name="id" label={t("agent.id")}>
            <Input disabled />
          </Form.Item>
        )}
        <Form.Item
          name="name"
          label={t("agent.name")}
          rules={[{ required: true, message: t("agent.nameRequired") }]}
        >
          <Input placeholder={t("agent.namePlaceholder")} />
        </Form.Item>
        <Form.Item name="description" label={t("agent.description")}>
          <Input.TextArea
            placeholder={t("agent.descriptionPlaceholder")}
            rows={3}
          />
        </Form.Item>
        <Form.Item
          hidden={selectedBackend !== "qwenpaw"}
          label={t("agent.model")}
          help={
            editingAgent
              ? t("agent.modelHelp")
              : globalActiveModel?.active_llm
                ? `账号可用模型：${globalActiveModel.active_llm.provider_id} / ${globalActiveModel.active_llm.model}`
                : "仅显示管理员为当前账号授权的模型。"
          }
        >
          <Space.Compact style={{ width: "100%" }}>
            <Select
              value={selectedProviderId || undefined}
              onChange={handleProviderChange}
              placeholder={t("agent.modelPlaceholder")}
              allowClear={!!editingAgent}
              onClear={editingAgent ? handleClearModel : undefined}
              loading={loadingProviders}
              style={{ width: "45%", gap: "8px" }}
              showSearch
              optionFilterProp="label"
              options={eligibleProviders.map((p) => {
                const isDefault =
                  globalActiveModel?.active_llm?.provider_id === p.id;
                return {
                  value: p.id,
                  label: isDefault ? `${p.name} (默认)` : p.name,
                };
              })}
              optionRender={({ value }) => {
                const p = eligibleProviders.find((ep) => ep.id === value);
                if (!p) return value;
                return (
                  <Space size={6}>
                    <ProviderIcon providerId={p.id} size={16} />
                    <span>{p.name}</span>
                  </Space>
                );
              }}
              notFoundContent={
                loadingProviders ? (
                  <Spin size="small" />
                ) : (
                  t("agent.noConfiguredModels")
                )
              }
            />
            <Select
              value={selectedModelId || undefined}
              onChange={(modelId) =>
                form.setFieldsValue({ active_model_model: modelId })
              }
              placeholder={
                selectedProviderId
                  ? t("models.model")
                  : t("agent.modelPlaceholder")
              }
              disabled={!selectedProviderId}
              style={{ width: "55%" }}
              showSearch
              optionFilterProp="label"
              options={availableModels.map((m) => {
                const isDefault =
                  globalActiveModel?.active_llm?.provider_id === selectedProviderId &&
                  globalActiveModel?.active_llm?.model === m.id;
                return {
                  value: m.id,
                  label: isDefault ? `${m.name || m.id} (默认)` : (m.name || m.id),
                };
              })}
            />
          </Space.Compact>
        </Form.Item>
        {editingAgent && (
          <Form.Item name="workspace_dir" label={t("agent.workspace")}>
            <Input disabled />
          </Form.Item>
        )}
      </Form>
    </Modal>
  );
}
