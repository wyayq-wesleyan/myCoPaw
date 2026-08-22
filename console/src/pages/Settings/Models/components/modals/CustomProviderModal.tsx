import { useState, useEffect } from "react";
import { Form, Input, Modal } from "@agentscope-ai/design";
import api from "../../../../../api";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "../../../../../hooks/useAppMessage";

interface CustomProviderModalProps {
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}

export function CustomProviderModal({
  open,
  onClose,
  onSaved,
}: CustomProviderModalProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

  useEffect(() => {
    if (open) {
      form.resetFields();
    }
  }, [open, form]);

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      setSaving(true);
      await api.createCustomProvider({
        id: values.id.trim(),
        name: values.name.trim(),
        default_base_url: values.default_base_url?.trim() || "",
        api_key: values.api_key?.trim() || "",
        api_key_prefix: "",
        chat_model: "OpenAIChatModel",
      });
      message.success(
        t("models.providerCreated", { name: values.name.trim() }),
      );
      onSaved();
      onClose();
    } catch (error) {
      if (error && typeof error === "object" && "errorFields" in error) return;
      const errMsg =
        error instanceof Error
          ? error.message
          : t("models.providerCreateFailed");
      message.error(errMsg);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={t("models.addProviderTitle")}
      open={open}
      onCancel={onClose}
      onOk={handleSubmit}
      confirmLoading={saving}
      okText={t("common.create")}
      cancelText={t("models.cancel")}
      destroyOnHidden
    >
      <Form
        form={form}
        layout="vertical"
        style={{ marginTop: 16 }}
        initialValues={{}}
      >
        <Form.Item
          name="id"
          label={t("models.providerIdLabel")}
          extra={t("models.providerIdHint")}
          rules={[
            { required: true, message: t("models.providerIdLabel") },
            {
              pattern: /^[a-z][a-z0-9_-]{0,63}$/,
              message: t("models.providerIdHint"),
            },
          ]}
        >
          <Input placeholder={t("models.providerIdPlaceholder")} />
        </Form.Item>

        <Form.Item
          name="name"
          label={t("models.providerNameLabel")}
          rules={[{ required: true, message: t("models.providerNameLabel") }]}
        >
          <Input placeholder={t("models.providerNamePlaceholder")} />
        </Form.Item>

        <Form.Item
          name="default_base_url"
          label={t("models.defaultBaseUrlLabel")}
          rules={[{ required: true, message: "请输入内网服务地址" }]}
        >
          <Input placeholder="例如：http://host.docker.internal:9988/v1" />
        </Form.Item>

        <Form.Item
          name="api_key"
          label="API Key"
          extra="内网服务未启用鉴权时可留空。"
        >
          <Input autoComplete="off" />
        </Form.Item>
      </Form>
    </Modal>
  );
}
