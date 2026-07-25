import { useEffect, useState } from "react";
import { Alert, Button, Checkbox, Form, Input, Modal, Popconfirm, Space, Table, Tag, message } from "antd";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { authApi, type ManagedUser, type ModelPermission } from "../../../api/modules/auth";
import { request } from "../../../api/request";

type PermissionKind = "model" | "skill" | "tool";
type PermissionTarget = { user: ManagedUser; kind: PermissionKind } | null;

export default function UserManagementPage() {
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [target, setTarget] = useState<PermissionTarget>(null);
  const [choices, setChoices] = useState<string[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [form] = Form.useForm();

  const load = async () => {
    setLoading(true);
    try { setUsers(await authApi.listUsers()); } catch (error) { message.error(error instanceof Error ? error.message : "无法加载用户列表"); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, []);

  const openPermissions = async (user: ManagedUser, kind: PermissionKind) => {
    setTarget({ user, kind });
    try {
      if (kind === "model") {
        const [providers, saved] = await Promise.all([request<any[]>("/models"), authApi.getUserModelPermissions(user.username)]);
        const all = providers.flatMap((provider) => [...(provider.models ?? []), ...(provider.extra_models ?? [])].map((model: any) => `${provider.id}::${model.id}`));
        setChoices(all);
        setSelected(saved.permissions === null ? all : saved.permissions.map((item) => `${item.provider_id}::${item.model_id}`));
      } else {
        const [items, saved] = await Promise.all([request<any[]>(kind === "skill" ? "/skills" : "/tools"), authApi.getUserNamedPermissions(user.username, kind)]);
        const all = items.map((item) => String(item.name)).filter(Boolean);
        setChoices(all);
        setSelected(all.filter((name) => !saved.disabled.includes(name)));
      }
    } catch (error) { message.error(error instanceof Error ? error.message : "加载授权失败"); setTarget(null); }
  };

  const savePermissions = async () => {
    if (!target) return;
    try {
      if (target.kind === "model") {
        const permissions: ModelPermission[] = selected.map((value) => { const [provider_id, model_id] = value.split("::"); return { provider_id, model_id }; });
        await authApi.setUserModelPermissions(target.user.username, permissions);
      } else {
        await authApi.setUserNamedPermissions(target.user.username, target.kind, choices.filter((name) => !selected.includes(name)));
      }
      message.success("授权已保存"); setTarget(null);
    } catch (error) { message.error(error instanceof Error ? error.message : "保存授权失败"); }
  };

  return <div style={{ padding: 24, maxWidth: 1180, margin: "0 auto" }}>
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}><div><h2 style={{ margin: 0 }}>用户管理</h2><span style={{ color: "var(--qwenpaw-color-text-description, #888)" }}>管理账户状态及模型、技能、工具的可用范围</span></div><Space><Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button><Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新增用户</Button></Space></div>
      <Alert type="info" showIcon message="默认全部开放。取消勾选后，该用户在页面和接口中均无法使用对应资源。" />
      <Table rowKey="username" loading={loading} dataSource={users} pagination={false} columns={[
        { title: "账户", dataIndex: "username" },
        { title: "角色", dataIndex: "role", render: (role) => <Tag color={role === "admin" ? "orange" : "blue"}>{role === "admin" ? "管理员" : "普通用户"}</Tag> },
        { title: "状态", dataIndex: "enabled", render: (enabled) => <Tag color={enabled ? "green" : "default"}>{enabled ? "已启用" : "已禁用"}</Tag> },
        { title: "授权", render: (_, user: ManagedUser) => user.role === "admin" ? "管理员拥有全部权限" : <Space><Button size="small" onClick={() => void openPermissions(user, "model")}>模型</Button><Button size="small" onClick={() => void openPermissions(user, "skill")}>技能</Button><Button size="small" onClick={() => void openPermissions(user, "tool")}>工具</Button></Space> },
        { title: "操作", render: (_, user: ManagedUser) => user.role === "admin" ? "-" : <Space><Button size="small" onClick={() => Modal.confirm({ title: `重置 ${user.username} 的密码`, content: <Input.Password id="reset-user-password" placeholder="新密码" />, onOk: async () => { const input = document.getElementById("reset-user-password") as HTMLInputElement; await authApi.setUserPassword(user.username, input.value); message.success("密码已重置"); } })}>重置密码</Button><Button size="small" onClick={async () => { await authApi.setUserStatus(user.username, !user.enabled); await load(); }}>{user.enabled ? "禁用" : "启用"}</Button><Popconfirm title="删除该用户及其私有工作区？" onConfirm={async () => { await authApi.deleteUser(user.username); await load(); }}><Button size="small" danger>删除</Button></Popconfirm></Space> },
      ]} />
    </Space>
    <Modal title="新增用户" open={createOpen} onCancel={() => setCreateOpen(false)} onOk={() => form.submit()} destroyOnClose><Form form={form} layout="vertical" onFinish={async (value) => { await authApi.createUser(value.username.trim(), value.password); message.success("用户已创建"); setCreateOpen(false); form.resetFields(); await load(); }}><Form.Item name="username" label="账户名" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="password" label="初始密码" rules={[{ required: true, min: 8 }]}><Input.Password /></Form.Item></Form></Modal>
    <Modal title={target ? `${target.user.username} 的${target.kind === "model" ? "模型" : target.kind === "skill" ? "技能" : "工具"}授权` : "授权"} open={!!target} onCancel={() => setTarget(null)} onOk={() => void savePermissions()} width={620}><Checkbox.Group value={selected} onChange={(values) => setSelected(values as string[])} style={{ width: "100%" }}><Space direction="vertical" style={{ width: "100%" }}>{choices.map((value) => <Checkbox key={value} value={value}>{value.replace("::", " - ")}</Checkbox>)}</Space></Checkbox.Group></Modal>
  </div>;
}
