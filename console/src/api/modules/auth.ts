import { getApiUrl } from "../config";
import { request } from "../request";

export interface LoginResponse {
  token: string;
  username: string;
  message?: string;
}

export interface AuthStatusResponse {
  enabled: boolean;
  has_users: boolean;
}

export interface ManagedUser {
  username: string;
  role: string;
  enabled: boolean;
  menu_permissions: string[] | null;
  skills: string[];
  tools: string[];
}
export interface ModelPermission { provider_id: string; model_id: string }
export interface AccountResourceItem {
  name: string;
  description: string;
  icon: string;
  resource_id: string;
  creator_username: string;
  source_label: string;
  is_builtin: boolean;
}
export interface UserResourcesResponse {
  username: string;
  skills: string[];
  tools: string[];
  available_skills: AccountResourceItem[];
  available_tools: AccountResourceItem[];
}
export interface CreateUserPayload {
  username: string;
  password: string;
  model_permissions?: ModelPermission[] | null;
  disabled_skills?: string[] | null;
  disabled_tools?: string[] | null;
  menu_permissions?: string[] | null;
  skills?: string[] | null;
  tools?: string[] | null;
}

export const authApi = {
  login: async (username: string, password: string): Promise<LoginResponse> => {
    const res = await fetch(getApiUrl("/auth/login"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Login failed");
    }
    return res.json();
  },

  register: async (
    username: string,
    password: string,
  ): Promise<LoginResponse> => {
    const res = await fetch(getApiUrl("/auth/register"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Registration failed");
    }
    return res.json();
  },

  getStatus: async (): Promise<AuthStatusResponse> => {
    const res = await fetch(getApiUrl("/auth/status"));
    if (!res.ok) throw new Error("Failed to check auth status");
    return res.json();
  },

  getCurrentUser: () => request<ManagedUser>("/auth/me"),
  listUsers: () => request<ManagedUser[]>("/auth/users"),
  createUser: (body: CreateUserPayload) =>
    request<ManagedUser>("/auth/users", { method: "POST", body: JSON.stringify(body) }),
  setUserStatus: (username: string, enabled: boolean) =>
    request(`/auth/users/${encodeURIComponent(username)}/status`, { method: "PATCH", body: JSON.stringify({ enabled }) }),
  setUserPassword: (username: string, password: string) =>
    request(`/auth/users/${encodeURIComponent(username)}/password`, { method: "PUT", body: JSON.stringify({ password }) }),
  deleteUser: (username: string) => request(`/auth/users/${encodeURIComponent(username)}`, { method: "DELETE" }),
  getUserModelPermissions: (username: string) => request<{ permissions: ModelPermission[] | null }>(`/auth/users/${encodeURIComponent(username)}/model-permissions`),
  setUserModelPermissions: (username: string, permissions: ModelPermission[] | null) =>
    request(`/auth/users/${encodeURIComponent(username)}/model-permissions`, { method: "PUT", body: JSON.stringify({ permissions }) }),
  getUserNamedPermissions: (username: string, kind: "skill" | "tool") =>
    request<{ disabled: string[] }>(`/auth/users/${encodeURIComponent(username)}/${kind}-permissions`),
  setUserNamedPermissions: (username: string, kind: "skill" | "tool", disabled: string[]) =>
    request(`/auth/users/${encodeURIComponent(username)}/${kind}-permissions`, { method: "PUT", body: JSON.stringify({ disabled }) }),
  getUserResources: (username: string) =>
    request<UserResourcesResponse>(
      `/auth/users/${encodeURIComponent(username)}/resources`,
    ),
  setUserResources: (
    username: string,
    resources: { skills: string[]; tools: string[] },
  ) =>
    request<UserResourcesResponse>(
      `/auth/users/${encodeURIComponent(username)}/resources`,
      { method: "PUT", body: JSON.stringify(resources) },
    ),
  getUserMenuPermissions: (username: string) =>
    request<{ permissions: string[] | null }>(`/auth/users/${encodeURIComponent(username)}/menu-permissions`),
  setUserMenuPermissions: (username: string, permissions: string[] | null) =>
    request(`/auth/users/${encodeURIComponent(username)}/menu-permissions`, {
      method: "PUT",
      body: JSON.stringify({ permissions }),
    }),

  updateProfile: async (
    currentPassword: string,
    newUsername?: string,
    newPassword?: string,
  ): Promise<LoginResponse> => {
    const token = localStorage.getItem("qwenpaw_auth_token") || "";
    const res = await fetch(getApiUrl("/auth/update-profile"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        current_password: currentPassword,
        new_username: newUsername || null,
        new_password: newPassword || null,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Update failed");
    }
    return res.json();
  },
};
