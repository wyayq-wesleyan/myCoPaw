import { useEffect, useState, useDeferredValue } from "react";
import { useNavigate } from "react-router-dom";
import { Card, Form, Modal, Table, Button, Tabs } from "@agentscope-ai/design";
import { Checkbox, Spin } from "antd";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useTranslation } from "react-i18next";
import {
  createColumns,
  FilterBar,
  SessionDrawer,
  formatTime,
  type Session,
} from "./components";
import { useSessions } from "./useSessions";
import api from "../../../api";
import { authApi } from "../../../api/modules/auth";
import { sessionApi } from "../../../api/modules/chat";
import { PageHeader } from "@/components/PageHeader";
import { ChannelIcon } from "../Channels/components";
import styles from "./index.module.less";

function SessionsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const {
    sessions,
    loading,
    updateSession,
    deleteSession,
    batchDeleteSessions,
    archiveSession,
    unarchiveSession,
    batchArchiveSessions,
    batchUnarchiveSessions,
    activeTab,
    setActiveTab,
    activeCount,
    archivedCount,
  } = useSessions();
  const [filteredSessions, setFilteredSessions] = useState<Session[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingSession, setEditingSession] = useState<Session | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm<Session>();

  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [shareSession, setShareSession] = useState<Session | null>(null);
  const [shareUsers, setShareUsers] = useState<string[]>([]);
  const [shareTargets, setShareTargets] = useState<string[]>([]);
  const [shareLoading, setShareLoading] = useState(false);

  // Filter states
  const [filterUserId, setFilterUserId] = useState<string>("");
  const [filterChannel, setFilterChannel] = useState<string>("");
  const [filterTitle, setFilterTitle] = useState<string>("");
  const [availableChannels, setAvailableChannels] = useState<string[]>([]);
  const [isMobile, setIsMobile] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia("(max-width: 768px)");
    setIsMobile(mq.matches);
    const handler = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  const deferredTitle = useDeferredValue(filterTitle);

  const { message } = useAppMessage();

  useEffect(() => {
    const fetchChannelTypes = async () => {
      try {
        const types = await api.listChannelTypes();
        setAvailableChannels(types);
      } catch (error) {
        console.error("Failed to load channel types:", error);
      }
    };
    fetchChannelTypes();
  }, []);

  // Filter effect
  useEffect(() => {
    let filtered: Session[] = sessions;

    if (filterUserId) {
      filtered = filtered.filter(
        (session: Session) =>
          session.user_id?.toLowerCase().includes(filterUserId.toLowerCase()),
      );
    }

    if (filterChannel) {
      filtered = filtered.filter(
        (session: Session) => session.channel === filterChannel,
      );
    }

    if (deferredTitle) {
      filtered = filtered.filter((session: Session) => {
        const name = session.name || "";
        return name.toLowerCase().includes(deferredTitle.toLowerCase());
      });
    }

    setFilteredSessions(filtered);
  }, [sessions, filterUserId, filterChannel, deferredTitle]);

  // Clear selection when switching tabs
  useEffect(() => {
    setSelectedRowKeys([]);
  }, [activeTab]);

  const handleEdit = (session: Session) => {
    setEditingSession(session);
    form.setFieldsValue(session as any);
    setDrawerOpen(true);
  };

  const handleDelete = (sessionId: string) => {
    Modal.confirm({
      title: t("sessions.confirmDelete"),
      content: t("sessions.deleteConfirm"),
      okText: t("cronJobs.deleteText"),
      okType: "primary",
      cancelText: t("cronJobs.cancelText"),
      onOk: async () => {
        await deleteSession(sessionId);
      },
    });
  };

  const handleView = (session: Session) => {
    navigate(`/chat/${encodeURIComponent(session.id)}`);
  };

  const handleArchiveToggle = async (session: Session) => {
    if (activeTab === "archived") {
      await unarchiveSession(session.id);
    } else {
      await archiveSession(session.id);
    }
  };

  const handleOpenShare = async (session: Session) => {
    setShareSession(session);
    setShareTargets([]);
    setShareLoading(true);
    try {
      const [targets, currentUser] = await Promise.all([
        api.listShareTargets(),
        authApi.getCurrentUser(),
      ]);
      setShareUsers(
        targets.filter((username) => username !== currentUser.username),
      );
    } catch (error) {
      message.error(error instanceof Error ? error.message : "无法加载用户列表");
      setShareSession(null);
    } finally {
      setShareLoading(false);
    }
  };

  const handleSubmitShare = async () => {
    if (!shareSession || shareTargets.length === 0) return;
    setShareLoading(true);
    try {
      const result = await sessionApi.shareSession(
        shareSession.id,
        shareTargets,
      );
      message.success(`已发送 ${result.created} 条会话审批请求`);
      setShareSession(null);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "会话分享失败");
    } finally {
      setShareLoading(false);
    }
  };

  const handleBatchDelete = () => {
    if (selectedRowKeys.length === 0) {
      message.warning(t("sessions.batchDeleteConfirm", { count: 0 }));
      return;
    }

    Modal.confirm({
      title: t("sessions.confirmDelete"),
      content: t("sessions.batchDeleteConfirm", {
        count: selectedRowKeys.length,
      }),
      okText: t("cronJobs.deleteText"),
      okType: "danger",
      cancelText: t("cronJobs.cancelText"),
      onOk: async () => {
        const success = await batchDeleteSessions(selectedRowKeys as string[]);
        if (success) {
          setSelectedRowKeys([]);
        }
      },
    });
  };

  const handleBatchArchive = async () => {
    if (selectedRowKeys.length === 0) return;
    const success = await batchArchiveSessions(selectedRowKeys as string[]);
    if (success) {
      setSelectedRowKeys([]);
    }
  };

  const handleBatchUnarchive = async () => {
    if (selectedRowKeys.length === 0) return;
    const success = await batchUnarchiveSessions(selectedRowKeys as string[]);
    if (success) {
      setSelectedRowKeys([]);
    }
  };

  const handleDrawerClose = () => {
    setDrawerOpen(false);
    setEditingSession(null);
  };

  const handleSubmit = async (values: Session) => {
    if (editingSession) {
      setSaving(true);
      try {
        const updated = {
          name: values.name,
        };
        const success = await updateSession(editingSession.id, updated);
        if (success) {
          setDrawerOpen(false);
        }
      } finally {
        setSaving(false);
      }
    }
  };

  const isArchivedTab = activeTab === "archived";

  const columns = createColumns({
    onEdit: handleEdit,
    onDelete: handleDelete,
    onView: handleView,
    onArchiveToggle: handleArchiveToggle,
    onShare: (session) => void handleOpenShare(session),
    isArchivedTab,
  });

  const rowSelection = {
    fixed: true,
    columnWidth: 50,
    selectedRowKeys,
    onChange: (newSelectedRowKeys: React.Key[]) => {
      setSelectedRowKeys(newSelectedRowKeys);
    },
  };

  return (
    <div className={styles.sessionsPage}>
      <PageHeader
        items={[{ title: t("nav.control") }, { title: t("sessions.title") }]}
        extra={
          <div className={styles.headerRight}>
            <FilterBar
              isMobile={isMobile}
              filterUserId={filterUserId}
              filterChannel={filterChannel}
              filterTitle={filterTitle}
              uniqueChannels={availableChannels}
              onUserIdChange={setFilterUserId}
              onChannelChange={setFilterChannel}
              onTitleChange={setFilterTitle}
            />
            {selectedRowKeys.length > 0 && (
              <>
                {isArchivedTab ? (
                  <Button onClick={handleBatchUnarchive}>
                    {t("sessions.archive.batchUnaction", "Batch Unarchive")} (
                    {selectedRowKeys.length})
                  </Button>
                ) : (
                  <Button onClick={handleBatchArchive}>
                    {t("sessions.archive.batchAction", "Batch Archive")} (
                    {selectedRowKeys.length})
                  </Button>
                )}
                <Button type="primary" danger onClick={handleBatchDelete}>
                  {t("sessions.batchDeleteButton")} ({selectedRowKeys.length})
                </Button>
              </>
            )}
          </div>
        }
      />

      <Tabs
        activeKey={activeTab}
        onChange={(key) => setActiveTab(key as "active" | "archived")}
        items={[
          {
            key: "active",
            label: `${t("sessions.activeTab", "Active")} (${activeCount})`,
          },
          {
            key: "archived",
            label: `${t(
              "sessions.archivedTab",
              "Archived",
            )} (${archivedCount})`,
          },
        ]}
        style={{ padding: "0 16px" }}
      />

      {isMobile ? (
        <div className={styles.mobileCardList}>
          {filteredSessions.map((session) => (
            <Card
              key={session.id}
              className={styles.mobileSessionCard}
              size="small"
              bodyStyle={{ padding: 24 }}
            >
              <div className={styles.mobileSessionHeader}>
                <span className={styles.mobileSessionName}>
                  {session.name || session.id}
                </span>
                <span className={styles.mobileSessionChannel}>
                  <ChannelIcon channelKey={session.channel} size={24} />
                </span>
              </div>
              <div className={styles.mobileSessionMeta}>
                <span>ID: {session.id}</span>
                {session.user_id && <span>User: {session.user_id}</span>}
                <span>Created: {formatTime(session.created_at)}</span>
              </div>
              <div className={styles.mobileSessionActions}>
                {isArchivedTab ? (
                  <>
                    <Button
                      size="small"
                      className={styles.mobileActionBtn}
                      onClick={() => handleArchiveToggle(session)}
                    >
                      {t("sessions.archive.unaction", "Unarchive")}
                    </Button>
                    <Button
                      size="small"
                      className={styles.mobileActionBtn}
                      onClick={() => void handleOpenShare(session)}
                    >
                      分享
                    </Button>
                    <Button
                      size="small"
                      className={styles.mobileActionBtn}
                      danger
                      onClick={() => handleDelete(session.id)}
                    >
                      {t("common.delete")}
                    </Button>
                  </>
                ) : (
                  <>
                    <Button
                      size="small"
                      className={styles.mobileActionBtn}
                      onClick={() => handleEdit(session)}
                    >
                      {t("common.edit")}
                    </Button>
                    <Button
                      size="small"
                      className={styles.mobileActionBtn}
                      onClick={() => handleView(session)}
                    >
                      {t("common.view")}
                    </Button>
                    <Button
                      size="small"
                      className={styles.mobileActionBtn}
                      onClick={() => handleArchiveToggle(session)}
                    >
                      {t("sessions.archive.action", "Archive")}
                    </Button>
                    <Button
                      size="small"
                      className={styles.mobileActionBtn}
                      onClick={() => void handleOpenShare(session)}
                    >
                      分享
                    </Button>
                    <Button
                      size="small"
                      className={styles.mobileActionBtn}
                      danger
                      onClick={() => handleDelete(session.id)}
                    >
                      {t("common.delete")}
                    </Button>
                  </>
                )}
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <Card className={styles.tableCard} bodyStyle={{ padding: 0 }}>
          <Table
            columns={columns}
            dataSource={filteredSessions}
            loading={loading}
            rowKey="id"
            rowSelection={rowSelection}
            rowClassName={(record) =>
              selectedRowKeys.includes(record.id) ? styles.selectedRow : ""
            }
            scroll={{ x: 1500 }}
            pagination={{
              pageSize: 10,
              showSizeChanger: false,
            }}
          />
        </Card>
      )}

      <SessionDrawer
        open={drawerOpen}
        editingSession={editingSession}
        form={form}
        saving={saving}
        onClose={handleDrawerClose}
        onSubmit={handleSubmit}
      />

      <Modal
        title={`分享会话：${shareSession?.name || ""}`}
        open={shareSession !== null}
        onCancel={() => setShareSession(null)}
        onOk={() => void handleSubmitShare()}
        okText="发送审批"
        cancelText={t("common.cancel")}
        confirmLoading={shareLoading}
        okButtonProps={{ disabled: shareTargets.length === 0 }}
      >
        <Spin spinning={shareLoading && shareUsers.length === 0}>
          <p>
            选择目标用户后，对方会在收件箱中审批；批准后会添加一份独立的会话副本，双方后续互不影响。
          </p>
          <Checkbox.Group
            value={shareTargets}
            onChange={(values) => setShareTargets(values.map(String))}
            style={{ display: "grid", gap: 10 }}
            options={shareUsers.map((username) => ({
              label: username,
              value: username,
            }))}
          />
        </Spin>
      </Modal>
    </div>
  );
}

export default SessionsPage;
