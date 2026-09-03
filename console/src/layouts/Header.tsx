import { Avatar, Dropdown, Layout, Space, Typography, type MenuProps } from "antd";
import { KeyOutlined, LogoutOutlined, UserOutlined } from "@ant-design/icons";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import styles from "./index.module.less";
import { Slot } from "../plugins/registry/Slot";
import { authApi, type ManagedUser } from "../api/modules/auth";
import { clearAuthToken, clearAuthUser } from "../api/config";

const { Header: AntHeader } = Layout;

/**
 * Offline deployments deliberately expose only local controls in the header.
 * Update checks, documentation and source-code links would either fail on an
 * intranet or send users to services that are not part of this product.
 */
export default function Header() {
  const navigate = useNavigate();
  const [user, setUser] = useState<ManagedUser | null>(null);

  useEffect(() => {
    authApi.getStatus().then((status) => {
      if (status.enabled) {
        void authApi.getCurrentUser().then(setUser).catch(() => setUser(null));
      }
    }).catch(() => setUser(null));
  }, []);

  const userMenu: MenuProps["items"] = [
    {
      key: "password",
      icon: <KeyOutlined />,
      label: "账户管理 / 修改密码",
      onClick: () => navigate("/user-management"),
    },
    { type: "divider" },
    {
      key: "logout",
      icon: <LogoutOutlined />,
      label: "退出登录",
      onClick: () => {
        clearAuthToken();
        clearAuthUser();
        window.location.href = "/login";
      },
    },
  ];

  return (
    <AntHeader className={styles.header}>
      <div className={styles.logoWrapper}>
        <Slot name="header.logo" kind="replace">
          <span className={styles.brandMark} aria-label="汇智小助手">
            <img src="/huizhi-favicon.svg" alt="" className={styles.logoImg} />
            <span className={styles.brandName}>汇智小助手</span>
          </span>
        </Slot>
      </div>
      <Slot name="header.left" kind="fill" />
      <Space className={styles.headerRight}>
        <Slot name="header.right" kind="fill" />
        {user && (
          <Dropdown menu={{ items: userMenu }} placement="bottomRight" trigger={["click"]}>
            <button type="button" className={styles.userMenuButton}>
              <Avatar size={28} icon={<UserOutlined />} />
              <Typography.Text className={styles.userMenuName}>{user.username}</Typography.Text>
            </button>
          </Dropdown>
        )}
      </Space>
    </AntHeader>
  );
}
