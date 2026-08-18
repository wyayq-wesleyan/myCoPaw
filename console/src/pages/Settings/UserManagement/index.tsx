import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Divider,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tag,
  message,
} from "antd";
import {
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import {
  authApi,
  type CreateUserPayload,
  type ManagedUser,
  type ModelPermission,
} from "../../../api/modules/auth";
import { request } from "../../../api/request";
import { clearAuthToken } from "../../../api/config";

type ResourceOptions = {
  models: string[];
  skills: string[];
  tools: string[];
};

type FormValues = {
  username: string;
  password?: string;
  models: string[];
  skills: string[];
  tools: string[];
  menus: string[];
};

type PasswordFormValues = {
  currentPassword: string;
  newPassword: string;
  confirmPassword: string;
};

const DEFAULT_MENUS = [
  "core.user-management",
  "core.agents",
  "core.files",
  "core.sessions",
  "core.cron-jobs",
  "core.token-usage",
];

const MENU_OPTIONS = [
  ["core.inbox", "收件箱与审批"],
  ["core.app-center", "应用中心"],
  ["core.sessions", "会话"],
  ["core.cron-jobs", "定时任务"],
  ["core.heartbeat", "心跳"],
  ["core.files", "文件"],
  ["core.skills", "技能"],
  ["core.tools", "工具"],
  ["core.mcp", "MCP"],
  ["core.acp", "ACP"],
  ["core.agent-config", "运行配置"],
  ["core.agent-stats", "智能体统计"],
  ["core.agents", "智能体管理"],
  ["core.skill-pool", "技能池"],
  ["core.environments", "环境变量"],
  ["core.offload-policy", "工具卸载策略"],
  ["core.security", "安全设置"],
  ["core.token-usage", "Token 消耗"],
  ["core.backups", "备份"],
  ["core.voice-transcription", "语音转写"],
  ["core.debug", "会话调试"],
  ["core.plugin-manager", "插件管理"],
] as const;

function selectionToModels(values: string[]): ModelPermission[] {
  return values.flatMap((value) => {
    const [provider_id, model_id] = value.split("::");
    return provider_id && model_id ? [{ provider_id, model_id }] : [];
  });
}

export default function UserManagementPage() {
  const [currentUser, setCurrentUser] = useState<ManagedUser | null>(null);
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState("");
  const [resources, setResources] = useState<ResourceOptions>({
    models: [],
    skills: [],
    tools: [],
  });
  const [dialog, setDialog] = useState<"create" | "edit" | null>(null);
  const [editingUser, setEditingUser] = useState<ManagedUser | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm<FormValues>();
  const [passwordForm] = Form.useForm<PasswordFormValues>();

  const load = async () => {
    setLoading(true);
    try {
      const current = await authApi.getCurrentUser();
      setCurrentUser(current);
      if (current.role !== "admin") {
        setUsers([]);
        return;
      }
      setUsers(await authApi.listUsers());
    } catch (error) {
      message.error(error instanceof Error ? error.message : "无法加载用户列表");
    } finally {
      setLoading(false);
    }
  };

  const loadResources = async (): Promise<ResourceOptions> => {
    const [providers, skills, tools] = await Promise.all([
      request<
        Array<{
          id: string;
          models?: Array<{ id: string }>;
          extra_models?: Array<{ id: string }>;
        }>
      >("/models"),
      request<Array<{ name?: string }>>("/skills"),
      request<Array<{ name?: string }>>("/tools"),
    ]);
    const next = {
      models: providers.flatMap((provider) =>
        [...(provider.models ?? []), ...(provider.extra_models ?? [])].map(
          (model) => `${provider.id}::${model.id}`,
        ),
      ),
      skills: skills.map((item) => item.name || "").filter(Boolean),
      tools: tools.map((item) => item.name || "").filter(Boolean),
    };
    setResources(next);
    return next;
  };

  useEffect(() => {
    void load();
  }, []);

  const openCreate = async () => {
    try {
      const options = await loadResources();
      setEditingUser(null);
      form.setFieldsValue({
        username: "",
        password: "",
        models: options.models,
        skills: options.skills,
        tools: options.tools,
        menus: DEFAULT_MENUS,
      });
      setDialog("create");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "无法加载可授权资源");
    }
  };

  const openEdit = async (user: ManagedUser) => {
    try {
      const [options, modelResult, skillResult, toolResult, menuResult] =
        await Promise.all([
          loadResources(),
          authApi.getUserModelPermissions(user.username),
          authApi.getUserNamedPermissions(user.username, "skill"),
          authApi.getUserNamedPermissions(user.username, "tool"),
          authApi.getUserMenuPermissions(user.username),
        ]);
      form.setFieldsValue({
        username: user.username,
        password: "",
        models:
          modelResult.permissions === null
            ? options.models
            : modelResult.permissions.map(
                (item) => `${item.provider_id}::${item.model_id}`,
              ),
        skills: options.skills.filter(
          (item) => !skillResult.disabled.includes(item),
        ),
        tools: options.tools.filter(
          (item) => !toolResult.disabled.includes(item),
        ),
        menus: menuResult.permissions ?? MENU_OPTIONS.map(([id]) => id),
      });
      setEditingUser(user);
      setDialog("edit");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "无法加载用户授权");
    }
  };

  const save = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      if (dialog === "create") {
        const payload: CreateUserPayload = {
          username: values.username.trim(),
          password: values.password || "",
          model_permissions: selectionToModels(values.models),
          disabled_skills: resources.skills.filter(
            (item) => !values.skills.includes(item),
          ),
          disabled_tools: resources.tools.filter(
            (item) => !values.tools.includes(item),
          ),
          menu_permissions: values.menus,
        };
        await authApi.createUser(payload);
        message.success("用户已创建");
      } else if (editingUser) {
        await Promise.all([
          authApi.setUserModelPermissions(
            editingUser.username,
            selectionToModels(values.models),
          ),
          authApi.setUserNamedPermissions(
            editingUser.username,
            "skill",
            resources.skills.filter((item) => !values.skills.includes(item)),
          ),
          authApi.setUserNamedPermissions(
            editingUser.username,
            "tool",
            resources.tools.filter((item) => !values.tools.includes(item)),
          ),
          authApi.setUserMenuPermissions(editingUser.username, values.menus),
          values.password
            ? authApi.setUserPassword(editingUser.username, values.password)
            : Promise.resolve(),
        ]);
        message.success("用户配置已更新");
      }
      setDialog(null);
      form.resetFields();
      await load();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "保存用户配置失败");
    } finally {
      setSaving(false);
    }
  };

  const visibleUsers = useMemo(() => {
    const normalized = keyword.trim().toLowerCase();
    return normalized
      ? users.filter((item) => item.username.toLowerCase().includes(normalized))
      : users;
  }, [keyword, users]);

  const changeOwnPassword = async () => {
    const values = await passwordForm.validateFields();
    setSaving(true);
    try {
      await authApi.updateProfile(
        values.currentPassword,
        undefined,
        values.newPassword,
      );
      message.success("密码已修改，请使用新密码重新登录");
      clearAuthToken();
      window.location.href = "/login";
    } catch (error) {
      message.error(error instanceof Error ? error.message : "密码修改失败");
    } finally {
      setSaving(false);
    }
  };

  if (!loading && currentUser?.role === "user") {
    return (
      <div style={{ padding: 24, maxWidth: 640, margin: "0 auto" }}>
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          <div>
            <h2 style={{ margin: 0 }}>用户管理</h2>
            <span style={{ color: "#697386" }}>修改当前账户的登录密码。</span>
          </div>
          <Form form={passwordForm} layout="vertical" onFinish={changeOwnPassword}>
            <Form.Item
              name="currentPassword"
              label="当前密码"
              rules={[{ required: true, message: "请输入当前密码" }]}
            >
              <Input.Password autoComplete="current-password" />
            </Form.Item>
            <Form.Item
              name="newPassword"
              label="新密码"
              rules={[
                { required: true, message: "请输入新密码" },
                { min: 8, message: "密码至少 8 位" },
              ]}
            >
              <Input.Password autoComplete="new-password" />
            </Form.Item>
            <Form.Item
              name="confirmPassword"
              label="确认新密码"
              dependencies={["newPassword"]}
              rules={[
                { required: true, message: "请再次输入新密码" },
                ({ getFieldValue }) => ({
                  validator(_, value) {
                    return value === getFieldValue("newPassword")
                      ? Promise.resolve()
                      : Promise.reject(new Error("两次输入的密码不一致"));
                  },
                }),
              ]}
            >
              <Input.Password autoComplete="new-password" />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={saving}>
              修改密码
            </Button>
          </Form>
        </Space>
      </div>
    );
  }

  const regularUser = editingUser?.role !== "admin";
  return (
    <div style={{ padding: 24, maxWidth: 1240, margin: "0 auto" }}>
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            gap: 16,
            alignItems: "center",
          }}
        >
          <div>
            <h2 style={{ margin: 0 }}>用户管理</h2>
            <span style={{ color: "#697386" }}>
              管理员统一分配模型、技能、工具和菜单权限。
            </span>
          </div>
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => void load()}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => void openCreate()}>
              新增用户
            </Button>
          </Space>
        </div>
        <Alert
          type="info"
          showIcon
          message="普通用户默认只显示对话、智能体、文件、会话、定时任务和 Token 消耗。模型供应商由管理员统一维护，用户只会看到被授权的模型。"
        />
        <Input
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          prefix={<SearchOutlined />}
          placeholder="按用户名搜索"
          allowClear
          style={{ maxWidth: 360 }}
        />
        <Table
          rowKey="username"
          loading={loading}
          dataSource={visibleUsers}
          pagination={{ pageSize: 10, showSizeChanger: false }}
          columns={[
            { title: "用户名", dataIndex: "username" },
            {
              title: "角色",
              dataIndex: "role",
              render: (role: string) => (
                <Tag color={role === "admin" ? "orange" : "blue"}>
                  {role === "admin" ? "管理员" : "普通用户"}
                </Tag>
              ),
            },
            {
              title: "状态",
              dataIndex: "enabled",
              render: (enabled: boolean) => (
                <Tag color={enabled ? "green" : "default"}>
                  {enabled ? "已启用" : "已禁用"}
                </Tag>
              ),
            },
            {
              title: "菜单数",
              render: (_: unknown, user: ManagedUser) =>
                user.role === "admin"
                  ? "全部"
                  : user.menu_permissions?.length ?? DEFAULT_MENUS.length,
            },
            {
              title: "操作",
              render: (_: unknown, user: ManagedUser) =>
                user.role === "admin" ? (
                  "-"
                ) : (
                  <Space>
                    <Button
                      size="small"
                      icon={<EditOutlined />}
                      onClick={() => void openEdit(user)}
                    >
                      编辑授权
                    </Button>
                    <Button
                      size="small"
                      onClick={async () => {
                        await authApi.setUserStatus(user.username, !user.enabled);
                        await load();
                      }}
                    >
                      {user.enabled ? "禁用" : "启用"}
                    </Button>
                    <Popconfirm
                      title="删除该用户及其独立工作区？"
                      onConfirm={async () => {
                        await authApi.deleteUser(user.username);
                        await load();
                      }}
                    >
                      <Button size="small" danger>
                        删除
                      </Button>
                    </Popconfirm>
                  </Space>
                ),
            },
          ]}
        />
      </Space>
      <Modal
        title={
          dialog === "create"
            ? "新增用户"
            : `编辑用户：${editingUser?.username || ""}`
        }
        open={dialog !== null}
        width={780}
        onCancel={() => setDialog(null)}
        onOk={() => void save()}
        confirmLoading={saving}
        okText={dialog === "create" ? "创建用户" : "保存修改"}
        destroyOnClose
      >
        <Form form={form} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item
            name="username"
            label="用户名"
            rules={[{ required: true, message: "请输入用户名" }]}
          >
            <Input disabled={dialog === "edit"} autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="password"
            label={dialog === "create" ? "初始密码" : "重置密码"}
            rules={
              dialog === "create"
                ? [{ required: true, min: 8, message: "密码至少 8 位" }]
                : []
            }
          >
            <Input.Password
              placeholder={dialog === "edit" ? "留空则不修改密码" : "至少 8 位"}
              autoComplete="new-password"
            />
          </Form.Item>
          {regularUser || dialog === "create" ? (
            <>
              <Divider orientation="left">可用模型</Divider>
              <Form.Item
                name="models"
                extra="只展示管理员在本机 OpenAI 兼容服务中配置过的模型。"
              >
                <Checkbox.Group
                  options={resources.models.map((value) => ({
                    label: value.replace("::", " / "),
                    value,
                  }))}
                />
              </Form.Item>
              <Divider orientation="left">技能与工具</Divider>
              <Form.Item name="skills" label="技能">
                <Checkbox.Group options={resources.skills} />
              </Form.Item>
              <Form.Item name="tools" label="工具">
                <Checkbox.Group options={resources.tools} />
              </Form.Item>
              <Divider orientation="left">可见菜单</Divider>
              <Form.Item
                name="menus"
                extra="对话和智能体选择器固定可见，不需要单独授权。"
              >
                <Checkbox.Group
                  options={MENU_OPTIONS.map(([value, label]) => ({
                    value,
                    label,
                  }))}
                />
              </Form.Item>
            </>
          ) : (
            <Alert
              type="info"
              message="管理员账户始终拥有全部模型、技能、工具和菜单权限。"
            />
          )}
        </Form>
      </Modal>
    </div>
  );
}
