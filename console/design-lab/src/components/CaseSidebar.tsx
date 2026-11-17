import { useEffect, useMemo, useState } from "react";
import {
  ArrowRightOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  IdcardOutlined,
  InfoCircleOutlined,
  LoadingOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PhoneOutlined,
  SearchOutlined,
  SwapOutlined,
  UnorderedListOutlined,
} from "@ant-design/icons";
import { Button, Checkbox, Input, Pagination, Popconfirm, Tooltip } from "antd";

import type { CaseRecord, TargetRecord } from "../mock";
import { CaseFileTree } from "./CaseFileTree";

interface CaseSidebarProps {
  cases: CaseRecord[];
  currentCase: CaseRecord;
  targets: TargetRecord[];
  targetsLoading: boolean;
  targetsRefreshedAt: string;
  onRefreshTargets: () => void;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onSelectCase: (caseRecord: CaseRecord) => void;
  onDeleteCases: (caseIds: string[]) => void;
}

type SidebarTab = "case" | "targets" | "files";

function targetIcon(type: TargetRecord["type"]) {
  if (type === "身份证") return <IdcardOutlined />;
  if (type === "车牌号") return <ArrowRightOutlined />;
  return <PhoneOutlined />;
}

export function CaseSidebar({
  cases,
  currentCase,
  targets,
  targetsLoading,
  targetsRefreshedAt,
  onRefreshTargets,
  collapsed,
  onToggleCollapsed,
  onSelectCase,
  onDeleteCases,
}: CaseSidebarProps) {
  const [activeTab, setActiveTab] = useState<SidebarTab>("case");
  const [pickingCase, setPickingCase] = useState(false);
  const [query, setQuery] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [targetPage, setTargetPage] = useState(1);

  const visibleTargets = targets.filter(
    (target) => target.status === "在控" || target.status === "待审批",
  );
  const targetPageSize = 3;
  const pagedTargets = visibleTargets.slice(
    (targetPage - 1) * targetPageSize,
    targetPage * targetPageSize,
  );

  const filteredCases = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return cases;
    return cases.filter((item) =>
      [item.code, item.name, item.owner, item.org, ...item.tags]
        .join(" ")
        .toLowerCase()
        .includes(keyword),
    );
  }, [cases, query]);

  const showPicker = pickingCase || query.trim().length > 0;
  const allSelected =
    filteredCases.length > 0 &&
    filteredCases.every((item) => selectedIds.includes(item.id));
  const someSelected = filteredCases.some((item) =>
    selectedIds.includes(item.id),
  );

  useEffect(() => {
    setTargetPage(1);
    setSelectedIds([]);
  }, [currentCase.id]);

  useEffect(() => {
    const maxPage = Math.max(
      1,
      Math.ceil(visibleTargets.length / targetPageSize),
    );
    if (targetPage > maxPage) setTargetPage(maxPage);
  }, [visibleTargets.length, targetPage]);

  const selectCase = (caseRecord: CaseRecord) => {
    onSelectCase(caseRecord);
    setPickingCase(false);
    setQuery("");
  };

  const togglePicker = () => {
    if (showPicker) {
      setPickingCase(false);
      setQuery("");
    } else {
      setPickingCase(true);
      setQuery("");
    }
  };

  const toggleSelected = (caseId: string) => {
    setSelectedIds((previous) =>
      previous.includes(caseId)
        ? previous.filter((id) => id !== caseId)
        : [...previous, caseId],
    );
  };

  const toggleSelectAll = () => {
    if (allSelected) {
      setSelectedIds((previous) =>
        previous.filter((id) => !filteredCases.some((item) => item.id === id)),
      );
    } else {
      setSelectedIds((previous) => [
        ...new Set([...previous, ...filteredCases.map((item) => item.id)]),
      ]);
    }
  };

  const invertSelection = () => {
    setSelectedIds((previous) => {
      const next = new Set(previous);
      filteredCases.forEach((item) => {
        if (next.has(item.id)) next.delete(item.id);
        else next.add(item.id);
      });
      return Array.from(next);
    });
  };

  const deleteSelected = () => {
    onDeleteCases(selectedIds);
    setSelectedIds([]);
    setPickingCase(false);
    setQuery("");
  };

  if (collapsed) {
    return (
      <aside className="dl-case-tree dl-case-tree-collapsed">
        <Tooltip title="展开案件栏" placement="right">
          <button
            className="dl-case-rail-toggle"
            type="button"
            onClick={onToggleCollapsed}
          >
            <MenuUnfoldOutlined />
          </button>
        </Tooltip>
        <div className="dl-case-rail-list">
          <Tooltip title="案件" placement="right">
            <button
              type="button"
              className={`dl-case-rail-item ${
                activeTab === "case" ? "dl-case-rail-item-active" : ""
              }`}
              onClick={() => {
                setActiveTab("case");
                onToggleCollapsed();
              }}
            >
              <InfoCircleOutlined />
            </button>
          </Tooltip>
          <Tooltip title="在控目标" placement="right">
            <button
              type="button"
              className={`dl-case-rail-item ${
                activeTab === "targets" ? "dl-case-rail-item-active" : ""
              }`}
              onClick={() => {
                setActiveTab("targets");
                onToggleCollapsed();
              }}
            >
              <IdcardOutlined />
            </button>
          </Tooltip>
          <Tooltip title="文件树" placement="right">
            <button
              type="button"
              className={`dl-case-rail-item ${
                activeTab === "files" ? "dl-case-rail-item-active" : ""
              }`}
              onClick={() => {
                setActiveTab("files");
                onToggleCollapsed();
              }}
            >
              <FolderOpenOutlined />
            </button>
          </Tooltip>
          <Tooltip title="切换案件" placement="right">
            <button
              type="button"
              className="dl-case-rail-item"
              onClick={() => {
                setActiveTab("case");
                onToggleCollapsed();
                setPickingCase(true);
                setQuery("");
              }}
            >
              <SwapOutlined />
            </button>
          </Tooltip>
        </div>
        <div className="dl-case-rail-count">{cases.length}</div>
      </aside>
    );
  }

  return (
    <aside className="dl-case-tree dl-case-sidebar">
      <div className="dl-case-sidebar-head">
        <div>
          <div className="dl-case-tree-kicker">案件工作区</div>
          <div className="dl-case-sidebar-title-row">
            <h2>案件</h2>
            <span className="dl-case-tree-count">{cases.length}</span>
          </div>
        </div>
        <Tooltip title="收起案件栏">
          <button
            className="dl-panel-collapse"
            type="button"
            onClick={onToggleCollapsed}
          >
            <MenuFoldOutlined />
          </button>
        </Tooltip>
      </div>

      <div className="dl-case-tabs">
        <button
          type="button"
          className={activeTab === "case" ? "dl-case-tab-active" : ""}
          onClick={() => setActiveTab("case")}
        >
          <FileTextOutlined />
          案件
        </button>
        <button
          type="button"
          className={activeTab === "targets" ? "dl-case-tab-active" : ""}
          onClick={() => {
            setActiveTab("targets");
            onRefreshTargets();
          }}
        >
          <IdcardOutlined />
          在控目标
          <span>{visibleTargets.length}</span>
        </button>
        <button
          type="button"
          className={activeTab === "files" ? "dl-case-tab-active" : ""}
          onClick={() => setActiveTab("files")}
        >
          <FolderOpenOutlined />
          文件树
        </button>
      </div>

      {activeTab === "case" ? (
        <>
          {showPicker ? (
            <div className="dl-case-sidebar-search">
              <Input
                allowClear
                autoFocus
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                prefix={<SearchOutlined />}
                placeholder="搜索案件编号、名称、主办人"
              />
            </div>
          ) : null}

          <div className="dl-case-sidebar-scroll">
            {showPicker ? (
              <div className="dl-case-picker">
                <div className="dl-case-picker-tools">
                  <Checkbox
                    checked={allSelected}
                    indeterminate={someSelected && !allSelected}
                    onChange={toggleSelectAll}
                  >
                    全选
                  </Checkbox>
                  <button type="button" onClick={invertSelection}>
                    反选
                  </button>
                  <Popconfirm
                    title={`删除 ${selectedIds.length} 个案件？`}
                    description="仅从当前工作台移除，不影响 案件系统影响的案件。"
                    okText="删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                    onConfirm={deleteSelected}
                    disabled={selectedIds.length === 0}
                  >
                    <Button
                      type="text"
                      danger
                      size="small"
                      disabled={selectedIds.length === 0}
                    >
                      删除 {selectedIds.length > 0 ? selectedIds.length : ""}
                    </Button>
                  </Popconfirm>
                </div>

                <div className="dl-case-picker-head">
                  <span>选择案件</span>
                  <span>{filteredCases.length} 个</span>
                </div>

                {filteredCases.length === 0 ? (
                  <div className="dl-case-picker-empty">没有匹配的案件</div>
                ) : (
                  filteredCases.map((item) => (
                    <div
                      key={item.id}
                      className={`dl-case-picker-row ${
                        item.id === currentCase.id
                          ? "dl-case-picker-row-active"
                          : ""
                      }`}
                    >
                      <Checkbox
                        checked={selectedIds.includes(item.id)}
                        onChange={() => toggleSelected(item.id)}
                      />
                      <button
                        type="button"
                        className="dl-case-picker-main"
                        onClick={() => selectCase(item)}
                      >
                        <span className="dl-case-picker-top">
                          <span className="dl-case-picker-code">
                            {item.code}
                          </span>
                          <span
                            className={`dl-case-status dl-case-status-${item.status}`}
                          >
                            {item.status}
                          </span>
                        </span>
                        <span className="dl-case-picker-name">{item.name}</span>
                        <span className="dl-case-picker-meta">
                          {item.org} · {item.owner} · 在控 {item.targets}
                        </span>
                      </button>
                    </div>
                  ))
                )}

                <button
                  type="button"
                  className="dl-switch-case dl-switch-case-inline"
                  onClick={togglePicker}
                >
                  <SwapOutlined /> 取消切换
                </button>
              </div>
            ) : (
              <>
                <div className="dl-case-detail">
                  <div className="dl-case-detail-top">
                    <span className="dl-case-code">{currentCase.code}</span>
                    <span
                      className={`dl-case-status dl-case-status-${currentCase.status}`}
                    >
                      {currentCase.status}
                    </span>
                  </div>
                  <h3>{currentCase.name}</h3>
                  <div className="dl-case-detail-meta">
                    <span>{currentCase.org}</span>
                    <span>主办人 {currentCase.owner}</span>
                    <span>立案 {currentCase.beginAt}</span>
                  </div>
                  <div className="dl-case-detail-kv">
                    <div>
                      <span>案件类型</span>
                      <strong>{currentCase.type}</strong>
                    </div>
                    <div>
                      <span>在控目标</span>
                      <strong>{currentCase.targets} 个</strong>
                    </div>
                    <div>
                      <span>最近更新</span>
                      <strong>{currentCase.updatedAt}</strong>
                    </div>
                  </div>
                  <div className="dl-case-detail-tags">
                    {currentCase.tags.map((tag) => (
                      <span key={tag}>{tag}</span>
                    ))}
                  </div>
                </div>

                <button
                  type="button"
                  className="dl-switch-case dl-switch-case-inline"
                  onClick={togglePicker}
                >
                  <SwapOutlined /> 切换案件
                </button>
              </>
            )}
          </div>
        </>
      ) : activeTab === "targets" ? (
        <div className="dl-case-sidebar-scroll dl-targets-tab">
          <div className="dl-side-section-head">
            <div>
              <span className="dl-side-section-title">在控目标</span>
              <span className="dl-side-section-sub">
                当前案件 {visibleTargets.length} 个
              </span>
              <span className="dl-side-section-refresh">
                {targetsLoading ? (
                  <>
                    <LoadingOutlined /> 刷新中…
                  </>
                ) : (
                  `更新于 ${targetsRefreshedAt}`
                )}
              </span>
            </div>
          </div>
          {visibleTargets.length === 0 ? (
            <div className="dl-case-picker-empty">当前案件没有在控目标</div>
          ) : (
            <>
              <div className="dl-side-target-list">
                {pagedTargets.map((target) => (
                  <div className="dl-side-target" key={target.id}>
                    <span className="dl-side-target-icon">
                      {targetIcon(target.type)}
                    </span>
                    <span className="dl-side-target-copy">
                      <strong>{target.name}</strong>
                      <small>
                        {target.type} {target.value}
                      </small>
                    </span>
                    <span
                      className={`dl-target-status dl-target-status-${target.status}`}
                    >
                      {target.status}
                    </span>
                  </div>
                ))}
              </div>
              {visibleTargets.length > targetPageSize ? (
                <Pagination
                  size="small"
                  current={targetPage}
                  pageSize={targetPageSize}
                  total={visibleTargets.length}
                  showSizeChanger={false}
                  onChange={setTargetPage}
                  className="dl-target-pagination"
                />
              ) : null}
            </>
          )}
        </div>
      ) : (
        <div className="dl-case-sidebar-scroll">
          <CaseFileTree caseCode={currentCase.code} />
        </div>
      )}
    </aside>
  );
}

export default CaseSidebar;
