import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Button, Form, Input } from "antd";
import { useAppMessage } from "../../hooks/useAppMessage";
import { LockOutlined, UserOutlined } from "@ant-design/icons";
import { authApi } from "../../api/modules/auth";
import { setAuthToken, setAuthUser } from "../../api/config";
import { useAgentStore } from "../../stores/agentStore";
import { useTheme } from "../../contexts/ThemeContext";
import { getPostLoginHref } from "../../utils/navigationMode";

export default function LoginPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { isDark } = useTheme();
  const [loading, setLoading] = useState(false);
  const [isRegister, setIsRegister] = useState(false);
  const [hasUsers, setHasUsers] = useState(true);
  const { message } = useAppMessage();
  const rawRedirect = searchParams.get("redirect") || "/chat";
  const redirect =
    rawRedirect.startsWith("/") && !rawRedirect.startsWith("//")
      ? rawRedirect
      : "/chat";

  const finishNavigation = useCallback(
    (target: string) => {
      // The agent store was created before the username was known — re-read
      // the per-user last-selected agent now that login has recorded it.
      useAgentStore.getState().hydrateFromStorage();
      const osHref = getPostLoginHref(window.location.pathname, target);
      if (osHref) {
        window.location.replace(osHref);
        return;
      }
      navigate(target, { replace: true });
    },
    [navigate],
  );

  useEffect(() => {
    authApi
      .getStatus()
      .then((res) => {
        if (!res.enabled) {
          finishNavigation(redirect);
          return;
        }
        setHasUsers(res.has_users);
        if (!res.has_users) {
          setIsRegister(true);
        }
      })
      .catch(() => {});
  }, [finishNavigation, redirect]);

  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true);
    try {
      const username = values.username.trim();
      if (isRegister) {
        const res = await authApi.register(username, values.password);
        if (res.token) {
          setAuthToken(res.token);
          setAuthUser(username);
          message.success(t("login.registerSuccess"));
          finishNavigation(redirect);
        }
      } else {
        const res = await authApi.login(username, values.password);
        if (res.token) {
          setAuthToken(res.token);
          setAuthUser(username);
          finishNavigation(redirect);
        } else {
          message.info(t("login.authNotEnabled"));
          finishNavigation(redirect);
        }
      }
    } catch (err) {
      let errorMsg = t("login.failed");

      // Check if it's an Error object and use the backend message directly
      if (err instanceof Error) {
        // Use the backend message directly without complex parsing
        errorMsg = err.message;
      } else if (isRegister) {
        errorMsg = t("login.registerFailed");
      }

      message.error(errorMsg);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        height: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: isDark
          ? "linear-gradient(135deg, #0b1d33 0%, #143c69 52%, #1b2b3d 100%)"
          : "linear-gradient(135deg, #f6f9fd 0%, #e7eff9 54%, #edf4fb 100%)",
        padding: 24,
      }}
    >
      <div
        style={{
          width: "min(400px, 100%)",
          padding: 34,
          borderRadius: 10,
          border: isDark
            ? "1px solid rgba(246, 232, 196, 0.16)"
            : "1px solid rgba(20, 60, 105, 0.14)",
          background: isDark ? "#10243b" : "rgba(255, 255, 255, 0.96)",
          boxShadow: isDark
            ? "0 20px 56px rgba(0, 0, 0, 0.34)"
            : "0 20px 52px rgba(20, 60, 105, 0.14)",
        }}
      >
        <div style={{ textAlign: "center", marginBottom: 32 }}>
          <img
            src="/huizhi-favicon.svg"
            alt="汇智小助手"
            style={{ height: 48, marginBottom: 12 }}
          />
          <h2
            style={{
              margin: 0,
              fontWeight: 650,
              fontSize: 22,
              color: isDark ? "#f6e8c4" : "#143c69",
            }}
          >
            {isRegister ? t("login.registerTitle") : t("login.title")}
          </h2>
          {!hasUsers && (
            <p
              style={{
                margin: "8px 0 0",
                color: isDark ? "rgba(230, 239, 250, 0.62)" : "#58708c",
                fontSize: 13,
              }}
            >
              {t("login.firstUserHint")}
            </p>
          )}
        </div>

        <Form
          layout="vertical"
          onFinish={onFinish}
          autoComplete="off"
          size="large"
        >
          <Form.Item
            name="username"
            rules={[{ required: true, message: t("login.usernameRequired") }]}
          >
            <Input
              prefix={
                <UserOutlined
                  style={{
                    color: isDark ? "rgba(255,255,255,0.45)" : undefined,
                  }}
                />
              }
              placeholder={t("login.usernamePlaceholder")}
              autoComplete="username"
              autoFocus
            />
          </Form.Item>

          <Form.Item
            name="password"
            rules={[{ required: true, message: t("login.passwordRequired") }]}
          >
            <Input.Password
              prefix={
                <LockOutlined
                  style={{
                    color: isDark ? "rgba(255,255,255,0.45)" : undefined,
                  }}
                />
              }
              placeholder={t("login.passwordPlaceholder")}
              autoComplete="current-password"
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 0, marginTop: 8 }}>
            <Button
              type="primary"
              htmlType="submit"
              loading={loading}
              block
              style={{ height: 44, borderRadius: 6, fontWeight: 600 }}
            >
              {isRegister ? t("login.register") : t("login.submit")}
            </Button>
          </Form.Item>
        </Form>
      </div>
    </div>
  );
}
