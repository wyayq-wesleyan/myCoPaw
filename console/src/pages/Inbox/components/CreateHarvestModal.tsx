import { useMemo, useState } from "react";
import { Modal, Form, Input, Select, Button, Card } from "antd";
import { Sparkles } from "lucide-react";
import type { HarvestTemplate } from "../types";
import styles from "./CreateHarvestModal.module.less";

interface CreateHarvestModalProps {
  open: boolean;
  onClose: () => void;
  onSubmit: (values: {
    name: string;
    keywords: string;
    templateId: string;
    frequency: string;
  }) => void;
}

const TEMPLATES: HarvestTemplate[] = [
  {
    id: "tech-daily",
    name: "前沿技术日报",
    emoji: "🚀",
    description: "每日AI、科技趋势和开源动态更新。",
    estimatedReadTime: 8,
    defaultSchedule: { cron: "0 9 * * *", timezone: "Asia/Shanghai" },
  },
  {
    id: "industry-weekly",
    name: "行业情报周报",
    emoji: "📊",
    description: "每周行业趋势深度分析。",
    estimatedReadTime: 15,
    defaultSchedule: { cron: "0 10 * * 1", timezone: "Asia/Shanghai" },
  },
  {
    id: "competitor-daily",
    name: "竞品动态监控",
    emoji: "🏢",
    description: "跟踪竞品动态和关键市场信号。",
    estimatedReadTime: 6,
    defaultSchedule: { cron: "0 18 * * *", timezone: "Asia/Shanghai" },
  },
];

export function CreateHarvestModal({
  open,
  onClose,
  onSubmit,
}: CreateHarvestModalProps) {
  const [form] = Form.useForm();
  const [selectedTemplateId, setSelectedTemplateId] = useState<string>("");

  const selectedTemplate = useMemo(
    () => TEMPLATES.find((template) => template.id === selectedTemplateId),
    [selectedTemplateId],
  );

  const handleSelectTemplate = (templateId: string) => {
    const template = TEMPLATES.find((item) => item.id === templateId);
    if (!template) return;
    setSelectedTemplateId(templateId);
    form.setFieldsValue({
      name: template.name,
      keywords: "",
      frequency: template.defaultSchedule.cron.includes("* * *")
        ? "daily"
        : "weekly",
    });
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={860}
      title={
        <span className={styles.modalTitle}>
          <Sparkles size={18} /> 创建信息采集
        </span>
      }
      destroyOnHidden
    >
      <div className={styles.templateGrid}>
        {TEMPLATES.map((template) => (
          <Card
            key={template.id}
            hoverable
            className={`${styles.templateCard} ${
              selectedTemplateId === template.id
                ? styles.templateCardActive
                : ""
            }`}
            onClick={() => handleSelectTemplate(template.id)}
          >
            <div className={styles.templateHeader}>
              <span className={styles.templateEmoji}>{template.emoji}</span>
              <strong>{template.name}</strong>
            </div>
            <p className={styles.templateDesc}>{template.description}</p>
            <span className={styles.templateMeta}>
              约{template.estimatedReadTime}分钟阅读
            </span>
          </Card>
        ))}
      </div>

      {selectedTemplate ? (
        <Form
          form={form}
          layout="vertical"
          onFinish={(values) =>
            onSubmit({
              ...values,
              templateId: selectedTemplate.id,
            })
          }
          className={styles.formSection}
        >
          <Form.Item
            name="name"
            label="采集名称"
            rules={[{ required: true, message: "请输入采集名称" }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="keywords"
            label="关键词"
            rules={[{ required: true, message: "请输入关键词" }]}
          >
            <Input.TextArea
              rows={3}
              placeholder="人工智能, 开源, 产品..."
            />
          </Form.Item>
          <Form.Item name="frequency" label="采集频率" initialValue="daily">
            <Select
              options={[
                { label: "每日", value: "daily" },
                { label: "每周", value: "weekly" },
              ]}
            />
          </Form.Item>
          <div className={styles.actions}>
            <Button onClick={onClose}>取消</Button>
            <Button type="primary" htmlType="submit">
              创建采集
            </Button>
          </div>
        </Form>
      ) : null}
    </Modal>
  );
}
