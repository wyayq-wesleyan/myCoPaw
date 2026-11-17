import { useState } from "react";
import {
  ArrowRightOutlined,
  CheckCircleFilled,
  LockOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { Button } from "antd";

import type { Role } from "../mock";

interface LoginPageProps {
  onLogin: (role: Role) => void;
}

export function LoginPage({ onLogin }: LoginPageProps) {
  const [loading, setLoading] = useState(false);

  const loginWithSso = () => {
    setLoading(true);
    window.setTimeout(() => {
      setLoading(false);
      onLogin("admin");
    }, 650);
  };

  return (
    <div className="dl-login">
      <section className="dl-login-brand">
        <div className="dl-login-brand-top">
          <div className="dl-brand-mark">汇</div>
          <div>
            <div className="dl-login-brand-name">汇智小助手</div>
            <div className="dl-login-brand-sub">智能业务工作台</div>
          </div>
        </div>

        <div className="dl-login-statement">
          <div className="dl-login-eyebrow">智能业务工作台</div>
          <h1>
            让核查回到案件，
            <br />
            让每一步都有依据。
          </h1>
          <p>
            统一身份、案件权限、查询服务与审批流转在同一条工作链路内完成。
            助手不绕过平台权限，所有查询结果可回溯、可审计。
          </p>
        </div>

        <div className="dl-login-grid">
          <div className="dl-login-proof">
            <CheckCircleFilled />
            <span>统一身份登录与组织同步</span>
          </div>
          <div className="dl-login-proof">
            <CheckCircleFilled />
            <span>案件在控目标权限校验</span>
          </div>
          <div className="dl-login-proof">
            <CheckCircleFilled />
            <span>审批流转与审计留痕</span>
          </div>
        </div>

        <div className="dl-login-brand-foot">
          <span className="dl-live-dot" />
          身份平台连接正常
          <span className="dl-login-env">内网环境</span>
        </div>
      </section>

      <section className="dl-login-panel">
        <div className="dl-login-card">
          <div className="dl-login-card-head">
            <div className="dl-login-icon">
              <SafetyCertificateOutlined />
            </div>
            <div>
              <h2>登录工作台</h2>
              <p>使用统一身份账号登录，菜单与权限由平台角色决定</p>
            </div>
          </div>

          <Button
            type="primary"
            size="large"
            block
            loading={loading}
            onClick={loginWithSso}
            className="dl-login-primary"
          >
            使用平台账号登录
            <ArrowRightOutlined />
          </Button>

          <div className="dl-login-note">
            <LockOutlined />
            登录后仅能访问平台授权的菜单与案件；无权限案件不会出现在案件树中。
          </div>

          <div className="dl-login-role-note">
            <div className="dl-login-role-title">角色说明</div>
            <div className="dl-login-role-row">
              <span className="dl-login-role-tag">管理角色</span>
              <span>
                可在平台侧维护智能体、知识空间、查询客户端，不参与案件业务。
              </span>
            </div>
            <div className="dl-login-role-row">
              <span className="dl-login-role-tag dl-login-role-tag-user">
                业务角色
              </span>
              <span>可使用信息核查、办公助手等授权业务。</span>
            </div>
            <div className="dl-login-role-hint">
              原型环境：默认登录账号同时具备管理角色，便于测试全部功能。
            </div>
          </div>
        </div>

        <div className="dl-login-foot">
          <span>汇智小助手 v2.2 · 设计原型</span>
          <span>身份平台不可用时无法登录</span>
        </div>
      </section>
    </div>
  );
}

export default LoginPage;
