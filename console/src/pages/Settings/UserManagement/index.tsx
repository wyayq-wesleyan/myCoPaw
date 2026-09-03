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
  Switch,
  Table,
  Tag,
  message,
  List,
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
import { clearAuthToken, clearAuthUser } from "../../../api/config";

type ResourceOptions = {
  models: string[];
  skills: import("../../../api/modules/auth").AccountResourceItem[];
  tools: import("../../../api/modules/auth").AccountResourceItem[];
  selectedSkills: string[];
  selectedTools: string[];
};

type FormValues = {
  username: string;
  password?: string;
  confirmPassword?: string;
  enabled: boolean;
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
  "core.knowledge",
  "core.data-analysis",
  "core.sessions",
  "core.cron-jobs",
  "core.token-usage",
  "core.skills",
  "core.tools",
  "core.inbox",
];

const MENU_OPTIONS = [
  ["core.inbox", "收件箱与审批"],
  ["core.app-center", "应用中心"],
  ["core.sessions", "会话"],
  ["core.cron-jobs", "定时任务"],
  ["core.heartbeat", "心跳"],
  ["core.files", "文件"],
  ["core.knowledge", "知识空间"],
  ["core.data-analysis", "数据分析"],
  ["core.skills", "技能"],
  ["core.tools", "工具"],
  ["core.mcp", "MCP"],
  ["core.acp", "ACP"],
  ["core.agent-config", "运行配置"],
  ["core.agent-stats", "智能体统计"],
  ["core.agents", "智能体管理"],
  ["core.environments", "环境变量"],
  ["core.offload-policy", "工具卸载策略"],
  ["core.security", "安全设置"],
  ["core.token-usage", "词元消耗"],
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
    selectedSkills: [],
    selectedTools: [],
  });
  const [dialog, setDialog] = useState<"create" | "edit" | null>(null);
  const [editingUser, setEditingUser] = useState<ManagedUser | null>(null);
  const [saving, setSaving] = useState(false);
  const [resourceSelection, setResourceSelection] = useState<{
    skills: string[];
    tools: string[];
  }>({ skills: [], tools: [] });
  const [resourceDialog, setResourceDialog] = useState<"skills" | "tools" | null>(null);
  const [resourceUser, setResourceUser] = useState<string>("");
  const [resourceSearch, setResourceSearch] = useState("");
  const [resourceSaving, setResourceSaving] = useState(false);
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

  const loadResources = async (username: string): Promise<ResourceOptions> => {
    const [providers, accountResources] = await Promise.all([
      request<
        Array<{
          id: string;
          models?: Array<{ id: string }>;
          extra_models?: Array<{ id: string }>;
        }>
      >("/models"),
      authApi.getUserResources(username),
    ]);
    const next = {
      models: providers.flatMap((provider) =>
        [...(provider.models ?? []), ...(provider.extra_models ?? [])].map(
          (model) => `${provider.id}::${model.id}`,
        ),
      ),
      skills: accountResources.available_skills,
      tools: accountResources.available_tools,
      selectedSkills: accountResources.skills,
      selectedTools: accountResources.tools,
    };
    setResources(next);
    return next;
  };

  useEffect(() => {
    void load();
  }, []);

  const openCreate = async () => {
    try {
      if (!currentUser) {
        throw new Error("当前登录状态已失效");
      }
      const options = await loadResources(currentUser.username);
      setEditingUser(null);
      form.setFieldsValue({
        username: "",
        password: "",
        confirmPassword: "",
        models: options.models,
        skills: options.selectedSkills,
        tools: options.selectedTools,
        menus: DEFAULT_MENUS,
      });
      setResourceSelection({
        skills: options.selectedSkills,
        tools: options.selectedTools,
      });
      setDialog("create");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "无法加载可授权资源");
    }
  };

  const openEdit = async (user: ManagedUser) => {
    try {
      const [options, accountResources, modelResult, menuResult] =
        await Promise.all([
          loadResources(user.username),
          authApi.getUserResources(user.username),
          authApi.getUserModelPermissions(user.username),
          authApi.getUserMenuPermissions(user.username),
        ]);
      form.setFieldsValue({
        username: user.username,
        password: "",
        confirmPassword: "",
        enabled: user.enabled,
        models:
          modelResult.permissions === null
            ? options.models
            : modelResult.permissions.map(
                (item) => `${item.provider_id}::${item.model_id}`,
              ),
        skills: accountResources.skills,
        tools: accountResources.tools,
        menus: Array.from(
          new Set([
            ...(menuResult.permissions ?? MENU_OPTIONS.map(([id]) => id)),
            "core.knowledge",
          ]),
        ),
      });
      setResourceSelection({
        skills: accountResources.skills,
        tools: accountResources.tools,
      });
      setEditingUser(user);
      setDialog("edit");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "无法加载用户授权");
    }
  };

  const openResourceDialog = async (kind: "skills" | "tools", username?: string) => {
    const target = username || resourceUser || users[0]?.username;
    if (!target) return;
    try {
      const accountResources = await authApi.getUserResources(target);
      setResourceUser(target);
      setResourceDialog(kind);
      setResourceSearch("");
      setResourceSelection({
        skills: accountResources.skills,
        tools: accountResources.tools,
      });
      setResources((current) => ({
        ...current,
        skills: accountResources.available_skills,
        tools: accountResources.available_tools,
      }));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "无法加载资源");
    }
  };

  const switchResourceUser = async (username: string) => {
    await openResourceDialog(resourceDialog || "skills", username);
  };

  const saveResourceDialog = async () => {
    if (!resourceDialog || !resourceUser) return;
    setResourceSaving(true);
    try {
      await authApi.setUserResources(resourceUser, {
        skills: resourceSelection.skills,
        tools: resourceSelection.tools,
      });
      message.success(`${resourceDialog === "skills" ? "技能" : "工具"}权限已更新`);
      setUsers(await authApi.listUsers());
      setResourceDialog(null);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "资源权限更新失败");
    } finally {
      setResourceSaving(false);
    }
  };

  const currentResourceItems = resourceDialog === "skills" ? resources.skills : resources.tools;
  const visibleResourceItems = currentResourceItems.filter((item) =>
    item.name.toLowerCase().includes(resourceSearch.trim().toLowerCase()),
  );
  const currentSelectedResources = resourceDialog === "skills"
    ? resourceSelection.skills
    : resourceSelection.tools;
  const setCurrentSelectedResources = (values: string[]) => {
    setResourceSelection((current) =>
      resourceDialog === "skills"
        ? { ...current, skills: values }
        : { ...current, tools: values },
    );
  };
  const setResourceGroupSelection = (
    items: import("../../../api/modules/auth").AccountResourceItem[],
    values: string[],
  ) => {
    const groupNames = new Set(items.map((item) => item.name));
    const unchanged = currentSelectedResources.filter((name) => !groupNames.has(name));
    setCurrentSelectedResources(Array.from(new Set([...unchanged, ...values])));
  };
  const selectAllVisibleResources = () => {
    setCurrentSelectedResources(
      Array.from(new Set([
        ...currentSelectedResources,
        ...visibleResourceItems.map((item) => item.name),
      ])),
    );
  };
  const invertVisibleResources = () => {
    const selected = new Set(currentSelectedResources);
    visibleResourceItems.forEach((item) => {
      if (selected.has(item.name)) selected.delete(item.name);
      else selected.add(item.name);
    });
    setCurrentSelectedResources(Array.from(selected));
  };

  const resourceGroups = [
    {
      title: resourceDialog === "skills" ? "初始技能" : "初始工具",
      hint: "系统预置资源，不可分享",
      items: visibleResourceItems.filter((item) => item.is_builtin),
      tone: "#eff6ff",
    },
    {
      title: resourceDialog === "skills" ? "后续新建技能" : "后续新建工具",
      hint: "用户创建或接收的资源",
      items: visibleResourceItems.filter((item) => !item.is_builtin),
      tone: "#f7f8fa",
    },
  ].filter((group) => group.items.length > 0);

  const save = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      if (dialog === "create") {
        const payload: CreateUserPayload = {
          username: values.username.trim(),
          password: values.password || "",
          model_permissions: selectionToModels(values.models),
          skills: resourceSelection.skills,
          tools: resourceSelection.tools,
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
          authApi.setUserResources(editingUser.username, {
            skills: resourceSelection.skills,
            tools: resourceSelection.tools,
          }),
          authApi.setUserMenuPermissions(editingUser.username, values.menus),
          values.password
            ? authApi.setUserPassword(editingUser.username, values.password)
            : Promise.resolve(),
          authApi.setUserStatus(editingUser.username, values.enabled),
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
      clearAuthUser();
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
          message="普通用户默认只显示对话、智能体、文件、会话、定时任务和词元消耗。模型供应商由管理员统一维护，用户只会看到被授权的模型。"
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
              title: "技能",
              render: (_: unknown, user: ManagedUser) => (
                <Button size="small" onClick={() => void openResourceDialog("skills", user.username)}>
                  技能（{user.skills?.length ?? 0}）
                </Button>
              ),
            },
            {
              title: "工具",
              render: (_: unknown, user: ManagedUser) => (
                <Button size="small" onClick={() => void openResourceDialog("tools", user.username)}>
                  工具（{user.tools?.length ?? 0}）
                </Button>
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
                      编辑
                    </Button>
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
        width={980}
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
          {dialog === "create" && (
            <Form.Item
              name="confirmPassword"
              label="确认初始密码"
              dependencies={["password"]}
              rules={[
                { required: true, message: "请再次输入初始密码" },
                ({ getFieldValue }) => ({
                  validator(_, value) {
                    return value === getFieldValue("password")
                      ? Promise.resolve()
                      : Promise.reject(new Error("两次输入的密码不一致"));
                  },
                }),
              ]}
            >
              <Input.Password autoComplete="new-password" />
            </Form.Item>
          )}
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
              <Divider orientation="left">账号技能与工具</Divider>
              {dialog === "create" ? (
                <Alert
                  type="info"
                  showIcon
                  message="新账号将继承管理员当前的技能和工具配置。创建完成后，可在用户列表中点击“编辑授权”进行调整。"
                  description={`当前将继承 ${resourceSelection.skills.length} 项技能、${resourceSelection.tools.length} 项工具。`}
                />
              ) : (
                <Alert
                  type="info"
                  showIcon
                  message="技能和工具请在用户列表的“技能”或“工具”按钮中统一管理。"
                  description="弹窗支持按用户查看，并提供搜索、全选和反选。"
                />
              )}
              <Divider orientation="left">可见菜单</Divider>
              <Form.Item
                name="menus"
                extra="对话和智能体选择器固定可见；知识空间对所有用户固定开放。"
              >
                <Checkbox.Group
                  options={MENU_OPTIONS.map(([value, label]) => ({
                    value,
                    label,
                    disabled: value === "core.knowledge",
                  }))}
                />
              </Form.Item>
              <Divider orientation="left">账号状态</Divider>
              <Form.Item name="enabled" label="启用状态" valuePropName="checked">
                <Switch checkedChildren="启用" unCheckedChildren="禁用" />
              </Form.Item>
              <Divider orientation="left" plain>
                <span style={{ color: "#ff4d4f" }}>危险操作</span>
              </Divider>
              <div style={{ display: "flex", gap: 12 }}>
                <Popconfirm
                  title="确定删除该用户及其独立工作区？此操作不可恢复。"
                  onConfirm={async () => {
                    if (!editingUser) return;
                    try {
                      await authApi.deleteUser(editingUser.username);
                      message.success("用户已删除");
                      setDialog(null);
                      form.resetFields();
                      await load();
                    } catch {
                      message.error("删除失败");
                    }
                  }}
                  okText="确定删除"
                  cancelText="取消"
                >
                  <Button danger>删除用户</Button>
                </Popconfirm>
              </div>
            </>
          ) : (
            <Alert
              type="info"
              message="管理员账户始终拥有全部模型、技能、工具和菜单权限。"
            />
          )}
        </Form>
      </Modal>
      <Modal
        title={`${resourceDialog === "skills" ? "技能" : "工具"}管理`}
        open={resourceDialog !== null}
        width={860}
        onCancel={() => setResourceDialog(null)}
        onOk={() => void saveResourceDialog()}
        confirmLoading={resourceSaving}
        okText="保存"
        cancelText="取消"
        destroyOnClose
      >
        <div style={{ display: "grid", gridTemplateColumns: "220px 1fr", minHeight: 420 }}>
          <div style={{ borderRight: "1px solid #edf0f4", paddingRight: 12 }}>
            <div style={{ fontWeight: 600, marginBottom: 10 }}>用户</div>
            <List
              size="small"
              dataSource={users}
              rowKey="username"
              renderItem={(user) => (
                <List.Item
                  style={{
                    cursor: "pointer",
                    padding: "10px 12px",
                    borderRadius: 6,
                    background: resourceUser === user.username ? "#eef5ff" : undefined,
                  }}
                  onClick={() => void switchResourceUser(user.username)}
                >
                  <span>{user.username}</span>
                  <Tag color={user.role === "admin" ? "orange" : "blue"}>
                    {user.role === "admin" ? "管理员" : "普通用户"}
                  </Tag>
                </List.Item>
              )}
            />
          </div>
          <div style={{ paddingLeft: 20 }}>
            <Space direction="vertical" size={12} style={{ width: "100%" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <strong>{resourceUser} 的{resourceDialog === "skills" ? "技能" : "工具"}</strong>
                <Space>
                  <Button size="small" onClick={selectAllVisibleResources}>全选</Button>
                  <Button size="small" onClick={invertVisibleResources}>反选</Button>
                </Space>
              </div>
              <Input
                allowClear
                value={resourceSearch}
                onChange={(event) => setResourceSearch(event.target.value)}
                placeholder="搜索资源名称"
              />
              <div style={{ maxHeight: 330, overflow: "auto", padding: "4px 2px" }}>
                <Space direction="vertical" size={12} style={{ width: "100%" }}>
                  {resourceGroups.map((group) => (
                    <div
                      key={group.title}
                      style={{
                        padding: "10px 12px 12px",
                        border: "1px solid #e6ebf2",
                        borderRadius: 8,
                        background: group.tone,
                      }}
                    >
                      <div style={{ marginBottom: 8 }}>
                        <strong>{group.title}</strong>
                        <span style={{ marginLeft: 8, color: "#8a94a6", fontSize: 12 }}>
                          {group.hint}
                        </span>
                      </div>
                      <Checkbox.Group
                        value={currentSelectedResources}
                        onChange={(values) =>
                          setResourceGroupSelection(group.items, values.map(String))
                        }
                        style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 12 }}
                        options={group.items.map((item) => ({
                          label: item.name,
                          value: item.name,
                        }))}
                      />
                    </div>
                  ))}
                </Space>
              </div>
              <span style={{ color: "#697386" }}>
                已选择 {currentSelectedResources.length} 项，共 {currentResourceItems.length} 项
              </span>
            </Space>
          </div>
        </div>
      </Modal>
    </div>
  );
}
