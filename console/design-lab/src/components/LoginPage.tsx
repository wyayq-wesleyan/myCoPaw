import { useState } from "react";
import {
  ArrowRightOutlined,
  AuditOutlined,
  DatabaseOutlined,
  LoadingOutlined,
  LockOutlined,
  SafetyCertificateOutlined,
  TeamOutlined,
} from "@ant-design/icons";

interface LoginPageProps {
  onLogin: () => void;
}

const points = [
  {
    icon: <SafetyCertificateOutlined />,
    title: "统一身份，一次登录",
    desc: "对接 MSP 单点登录，组织、角色、菜单、权限自动同步。",
  },
  {
    icon: <DatabaseOutlined />,
    title: "案件贯通，数据可用",
    desc: "按账号加载所属案件，知识、数据与研判过程围绕案件展开。",
  },
  {
    icon: <AuditOutlined />,
    title: "全程留痕，可追溯",
    desc: "登录、查询、导入、导出与审批动作统一写入审计日志。",
  },
];

export function LoginPage({ onLogin }: LoginPageProps) {
  const [loading, setLoading] = useState(false);

  const handleLogin = () => {
    if (loading) return;
    setLoading(true);
    window.setTimeout(onLogin, 700);
  };

  return (
    <div className="dl-login">
      <section className="dl-login-brand">
        <div className="dl-login-pattern" />
        <div className="dl-login-brand-inner">
          <div className="dl-login-logo">
            <div className="dl-brand-mark">汇</div>
            <div>
              <div className="dl-login-logo-copy">汇智小助手</div>
              <div className="dl-login-logo-sub">公安内网 · 智能研判工作台</div>
            </div>
          </div>

          <h1 className="dl-login-title">
            让研判回到
            <br />
            <em>案件与证据</em>本身
          </h1>
          <p className="dl-login-sub">
            面向警务实战的智能工作台。统一身份接入、案件上下文贯通、
            数据与知识受控使用，所有操作全程留痕。
          </p>

          <div className="dl-login-points">
            {points.map((point) => (
              <div className="dl-login-point" key={point.title}>
                <div className="dl-login-point-icon">{point.icon}</div>
                <div className="dl-login-point-copy">
                  <strong>{point.title}</strong>
                  <span>{point.desc}</span>
                </div>
              </div>
            ))}
          </div>

          <div className="dl-login-footer">
            内部系统 · 仅限授权人员使用 · 所有操作均记录审计日志
          </div>
        </div>
      </section>

      <section className="dl-login-panel">
        <div className="dl-login-card">
          <div className="dl-login-badge">
            <LockOutlined /> 统一身份认证
          </div>
          <h2 className="dl-login-card-title">登录工作台</h2>
          <p className="dl-login-card-sub">
            使用 MSP 统一账号登录。系统将自动同步您的组织、角色与菜单权限，
            无需在汇智小助手单独维护账号。
          </p>

          <button
            className="dl-sso-button"
            type="button"
            onClick={handleLogin}
            disabled={loading}
          >
            {loading ? <LoadingOutlined /> : <SafetyCertificateOutlined />}
            {loading ? "正在连接统一身份平台…" : "使用平台账号登录"}
            {!loading ? <ArrowRightOutlined /> : null}
          </button>

          <div className="dl-login-status">
            <span className="dl-login-status-dot" />
            <span>平台连接正常</span>
            <span style={{ marginLeft: "auto", color: "#9b9286" }}>
              MSP / TCMS / ELC
            </span>
          </div>

          <div className="dl-login-secondary">
            <a onClick={(event) => event.preventDefault()}>管理员应急入口</a>
            <a onClick={(event) => event.preventDefault()}>登录遇到问题？</a>
          </div>

          <div className="dl-login-note">
            登录即代表您知悉并同意：本系统仅用于授权范围内的警务工作，
            登录、查询、导入、导出与审批动作将写入执法监督日志。
          </div>
        </div>
      </section>
    </div>
  );
}

export default LoginPage;
