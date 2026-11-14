import {
  CheckCircleFilled,
  DatabaseOutlined,
  FileTextOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { Tabs } from "antd";

import type { CaseRecord, ReferenceItem } from "../mock";
import { approvalRecord, caseInfo } from "../mock";

interface ContextPanelProps {
  currentCase: CaseRecord;
  references: ReferenceItem[];
}

function referenceIcon(kind: ReferenceItem["kind"]) {
  return kind === "data_table" ? <DatabaseOutlined /> : <FileTextOutlined />;
}

export function ContextPanel({ currentCase, references }: ContextPanelProps) {
  const caseTab = (
    <div className="dl-kv">
      {caseInfo.map((item) => (
        <div className="dl-kv-row" key={item.label}>
          <div className="dl-kv-label">{item.label}</div>
          <div className="dl-kv-value">{item.value}</div>
        </div>
      ))}
    </div>
  );

  const referenceTab = (
    <div className="dl-context-ref">
      {references.map((reference) => (
        <div className="dl-context-ref-item" key={reference.id}>
          <span className="dl-context-ref-icon">
            {referenceIcon(reference.kind)}
          </span>
          <div className="dl-context-ref-copy">
            <div className="dl-context-ref-name">{reference.name}</div>
            <div className="dl-context-ref-meta">{reference.meta}</div>
          </div>
        </div>
      ))}
    </div>
  );

  const approvalTab = (
    <div className="dl-approval-card">
      <span className="dl-approval-status">
        <CheckCircleFilled />
        {approvalRecord.status}
      </span>
      <div className="dl-approval-hint">{approvalRecord.hint}</div>
      <div className="dl-approval-rule">{approvalRecord.rule}</div>
      <button
        type="button"
        style={{
          marginTop: 12,
          padding: 0,
          border: 0,
          background: "transparent",
          color: "#24527d",
          fontSize: 11,
          cursor: "pointer",
        }}
      >
        查看审批规则 →
      </button>
    </div>
  );

  return (
    <aside className="dl-context">
      <div className="dl-context-head">
        <div className="dl-context-title">案件上下文</div>
        <div className="dl-context-case">
          <span className="dl-case-code">{currentCase.code}</span>
          <span className="dl-context-case-name">{currentCase.name}</span>
        </div>
      </div>

      <div className="dl-context-tabs">
        <Tabs
          items={[
            { key: "case", label: "案件信息", children: caseTab },
            { key: "refs", label: "参考依据", children: referenceTab },
            { key: "approval", label: "审批", children: approvalTab },
          ]}
          size="small"
        />
      </div>

      <div className="dl-context-stats">
        <div className="dl-stat">
          <div className="dl-stat-value">3</div>
          <div className="dl-stat-label">工具调用</div>
        </div>
        <div className="dl-stat">
          <div className="dl-stat-value">{references.length}</div>
          <div className="dl-stat-label">参考依据</div>
        </div>
        <div className="dl-stat">
          <div className="dl-stat-value">0</div>
          <div className="dl-stat-label">审批事项</div>
        </div>
      </div>
    </aside>
  );
}

export default ContextPanel;
