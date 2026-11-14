import { useMemo, useState } from "react";
import {
  ArrowRightOutlined,
  FolderOpenOutlined,
  ReloadOutlined,
  SearchOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import { Button, Input, Select } from "antd";

import type { CaseRecord } from "../mock";
import { cases } from "../mock";

interface CasePageProps {
  onOpenChat: (caseRecord: CaseRecord) => void;
}

const statusClass: Record<CaseRecord["status"], string> = {
  在办: "dl-status-active",
  待审批: "dl-status-pending",
  已结案: "dl-status-closed",
};

export function CasePage({ onOpenChat }: CasePageProps) {
  const [keyword, setKeyword] = useState("");
  const [status, setStatus] = useState<string>("全部");
  const [type, setType] = useState<string>("全部");

  const filtered = useMemo(() => {
    const lower = keyword.trim().toLowerCase();
    return cases.filter((caseRecord) => {
      const matchesKeyword =
        !lower ||
        caseRecord.code.toLowerCase().includes(lower) ||
        caseRecord.name.toLowerCase().includes(lower) ||
        caseRecord.owner.toLowerCase().includes(lower);
      const matchesStatus =
        status === "全部" || caseRecord.status === status;
      const matchesType = type === "全部" || caseRecord.type === type;
      return matchesKeyword && matchesStatus && matchesType;
    });
  }, [keyword, status, type]);

  const stats = {
    total: cases.length,
    active: cases.filter((item) => item.status === "在办").length,
    pending: cases.filter((item) => item.status === "待审批").length,
    closed: cases.filter((item) => item.status === "已结案").length,
  };

  return (
    <div className="dl-page">
      <div className="dl-page-head">
        <div>
          <h1 className="dl-page-title">案件工作台</h1>
          <div className="dl-page-sub">
            平台同步案件 · 按当前账号权限展示 · 点击案件进入研判
          </div>
        </div>
        <div className="dl-page-actions">
          <Button icon={<SyncOutlined />}>同步平台案件</Button>
          <Button icon={<ReloadOutlined />}>刷新</Button>
        </div>
      </div>

      <div className="dl-stats">
        <div className="dl-stat-card">
          <div className="dl-stat-label">我的案件</div>
          <div className="dl-stat-value">{stats.total}</div>
          <div className="dl-stat-hint">来自 TCMS 当前账号权限</div>
        </div>
        <div className="dl-stat-card">
          <div className="dl-stat-label">在办</div>
          <div className="dl-stat-value">{stats.active}</div>
          <div className="dl-stat-hint">可进入研判与数据查询</div>
        </div>
        <div className="dl-stat-card">
          <div className="dl-stat-label">待审批</div>
          <div className="dl-stat-value">{stats.pending}</div>
          <div className="dl-stat-hint">存在未完成的 TCMS 流程</div>
        </div>
        <div className="dl-stat-card">
          <div className="dl-stat-label">已结案</div>
          <div className="dl-stat-value">{stats.closed}</div>
          <div className="dl-stat-hint">默认只读，禁止新增材料</div>
        </div>
      </div>

      <div className="dl-filter-bar">
        <Input
          allowClear
          prefix={<SearchOutlined style={{ color: "#9a9185" }} />}
          placeholder="搜索案件编号、名称或主办人"
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          style={{ width: 300 }}
        />
        <Select
          value={status}
          onChange={setStatus}
          options={["全部", "在办", "待审批", "已结案"].map((value) => ({
            value,
            label: `状态：${value}`,
          }))}
          style={{ width: 140 }}
        />
        <Select
          value={type}
          onChange={setType}
          options={["全部", "刑事案件", "情报案件", "行政案件"].map((value) => ({
            value,
            label: `类型：${value}`,
          }))}
          style={{ width: 150 }}
        />
        <span style={{ marginLeft: "auto", color: "#8a8275", fontSize: 11 }}>
          共 {filtered.length} 个案件
        </span>
      </div>

      <div className="dl-panel">
        <div className="dl-panel-head">
          <FolderOpenOutlined style={{ color: "#8a6c3f" }} />
          <span className="dl-panel-title">案件列表</span>
          <span className="dl-panel-count">按更新时间倒序</span>
        </div>
        <table className="dl-table">
          <thead>
            <tr>
              <th style={{ width: "30%" }}>案件</th>
              <th>类型</th>
              <th>状态</th>
              <th>主办人</th>
              <th>主办单位</th>
              <th>更新时间</th>
              <th style={{ width: 84 }}>操作</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((caseRecord) => (
              <tr
                className="dl-table-row"
                key={caseRecord.id}
                onClick={() => onOpenChat(caseRecord)}
              >
                <td>
                  <div className="dl-table-case">
                    <span className="dl-table-code">{caseRecord.code}</span>
                    <span className="dl-table-name">{caseRecord.name}</span>
                  </div>
                </td>
                <td>{caseRecord.type}</td>
                <td>
                  <span
                    className={`dl-status ${statusClass[caseRecord.status]}`}
                  >
                    {caseRecord.status}
                  </span>
                </td>
                <td>{caseRecord.owner}</td>
                <td>{caseRecord.org}</td>
                <td style={{ color: "#7a828b", fontSize: 11 }}>
                  {caseRecord.updatedAt}
                </td>
                <td>
                  <span
                    className="dl-table-action"
                    onClick={(event) => {
                      event.stopPropagation();
                      onOpenChat(caseRecord);
                    }}
                  >
                    进入研判 <ArrowRightOutlined style={{ fontSize: 10 }} />
                  </span>
                </td>
              </tr>
            ))}
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={7}>
                  <div
                    style={{
                      padding: "40px 0",
                      textAlign: "center",
                      color: "#9a9185",
                      fontSize: 12,
                    }}
                  >
                    没有匹配的案件。请调整筛选条件，或检查平台案件权限。
                  </div>
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default CasePage;
