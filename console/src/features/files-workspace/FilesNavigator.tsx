import {
  DndContext,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { Checkbox, Dropdown, Input, Modal, Progress, Switch } from "antd";
import {
  ArrowLeftRight,
  ChevronDown,
  ChevronRight,
  File,
  FileCode2,
  FileText,
  Folder,
  FolderOpen,
  GripVertical,
  ListChecks,
  LoaderCircle,
  MoreHorizontal,
  Network,
  Pencil,
  Settings2,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { workspaceApi } from "../../api/modules/workspace";
import { chatProjectDirectoryApi } from "../../api/modules/chatProjectDirectory";
import { projectDirectoryApi } from "../../api/modules/projectDirectory";
import { useAppMessage } from "../../hooks/useAppMessage";
import { useCodingTabsStore } from "../../stores/codingTabsStore";
import SessionProjectDirectory from "../project-directory/SessionProjectDirectory";
import { getPendingProjectDirectory } from "../project-directory/pendingProjectDirectory";
import { directoriesMatch, workspaceRoots } from "./directorySources";
import { isDefaultWorkspaceMarkdown } from "./defaultWorkspaceMarkdown";
import {
  filesWorkspaceScopeKey,
  type FilesWorkspaceScope,
} from "./filesWorkspaceScope";
import {
  buildDailyMemoryTree,
  buildMemoryTree,
  type MemoryTreeEntry,
} from "./memoryTree";
import type {
  DirectoryEntry,
  FileTarget,
  MemoryGraphRoot,
  WorkspaceRoot,
} from "./types";
import styles from "./FilesWorkspace.module.less";

interface DirectoryNodeProps {
  entry: DirectoryEntry;
  chatId?: string;
  projectDirOverride?: string;
  selectedPath: string;
  onSelect: (target: FileTarget) => void;
  depth: number;
  root: WorkspaceRoot;
  initiallyExpanded?: boolean;
  refreshToken?: number;
  canManageEntry?: (path: string) => boolean;
  onRenameEntry?: (entry: DirectoryEntry) => void;
  onDeleteEntry?: (entry: DirectoryEntry) => void;
  selectionMode?: boolean;
  selectedPaths?: Set<string>;
  onToggleSelection?: (entry: DirectoryEntry) => void;
}

interface ProfileFileRowProps {
  entry: DirectoryEntry;
  enabled: boolean;
  selected: boolean;
  onSelect: () => void;
  onToggle: () => void;
}

type NavigatorSource = "workspace" | "profile" | "daily" | "digest";

function formatBytes(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1,
  );
  const value = bytes / 1024 ** index;
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function FileGlyph({ name }: { name: string }) {
  const extension = name.split(".").pop()?.toLowerCase();
  if (["md", "mdx", "txt", "log", "csv"].includes(extension ?? "")) {
    return <FileText size={15} />;
  }
  if (
    [
      "py",
      "ts",
      "tsx",
      "js",
      "jsx",
      "go",
      "rs",
      "java",
      "html",
      "css",
    ].includes(extension ?? "")
  ) {
    return <FileCode2 size={15} />;
  }
  return <File size={15} />;
}

function ProfileFileRow({
  entry,
  enabled,
  selected,
  onSelect,
  onToggle,
}: ProfileFileRowProps) {
  const { t } = useTranslation();
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({
    id: entry.path,
    disabled: !enabled,
  });

  return (
    <div
      ref={setNodeRef}
      className={`${styles.profileRow} ${
        selected ? styles.treeRowSelected : ""
      } ${isDragging ? styles.profileRowDragging : ""}`}
      style={{
        transform: CSS.Transform.toString(transform),
        transition,
      }}
    >
      <button type="button" className={styles.profileOpen} onClick={onSelect}>
        {enabled && (
          <span
            className={styles.dragHandle}
            {...attributes}
            {...listeners}
            onClick={(event) => event.stopPropagation()}
          >
            <GripVertical size={13} />
          </span>
        )}
        <FileGlyph name={entry.name} />
        <span>{entry.name}</span>
      </button>
      <Switch
        size="small"
        checked={enabled}
        aria-label={t("files.promptToggle", { name: entry.name })}
        onClick={(_checked, event) => {
          event.stopPropagation();
          onToggle();
        }}
      />
    </div>
  );
}

function EntryActions({
  entry,
  onRename,
  onDelete,
}: {
  entry: DirectoryEntry;
  onRename: (entry: DirectoryEntry) => void;
  onDelete: (entry: DirectoryEntry) => void;
}) {
  const { t } = useTranslation();
  return (
    <Dropdown
      trigger={["click"]}
      menu={{
        items: [
          {
            key: "rename",
            icon: <Pencil size={14} />,
            label: t("files.rename"),
          },
          {
            key: "delete",
            icon: <Trash2 size={14} />,
            label: t("files.delete"),
            danger: true,
          },
        ],
        onClick: ({ key, domEvent }) => {
          domEvent.stopPropagation();
          if (key === "rename") onRename(entry);
          if (key === "delete") onDelete(entry);
        },
      }}
    >
      <button
        type="button"
        className={styles.entryActionsButton}
        aria-label={t("files.manageEntry", { name: entry.name })}
        title={t("files.manageEntry", { name: entry.name })}
        onClick={(event) => event.stopPropagation()}
      >
        <MoreHorizontal size={14} />
      </button>
    </Dropdown>
  );
}

function DirectoryNode({
  entry,
  chatId,
  projectDirOverride,
  selectedPath,
  onSelect,
  depth,
  root,
  initiallyExpanded = false,
  refreshToken = 0,
  canManageEntry,
  onRenameEntry,
  onDeleteEntry,
  selectionMode = false,
  selectedPaths,
  onToggleSelection,
}: DirectoryNodeProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const [loading, setLoading] = useState(false);
  const [children, setChildren] = useState<DirectoryEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const autoExpandedRef = useRef(false);

  const load = useCallback(
    async (nextCursor?: string) => {
      setLoading(true);
      try {
        const page = await workspaceApi.listDirectory(
          entry.path,
          nextCursor,
          200,
          chatId,
          root,
          projectDirOverride,
        );
        setChildren((current) =>
          nextCursor ? [...current, ...page.entries] : page.entries,
        );
        setCursor(page.next_cursor);
        setHasMore(page.has_more);
      } finally {
        setLoading(false);
      }
    },
    [chatId, entry.path, projectDirOverride, root],
  );

  useEffect(() => {
    if (!initiallyExpanded || autoExpandedRef.current) return;
    autoExpandedRef.current = true;
    setExpanded(true);
    if (children.length === 0) void load();
  }, [children.length, initiallyExpanded, load]);

  const lastRefreshRef = useRef(refreshToken);
  useEffect(() => {
    if (lastRefreshRef.current === refreshToken) return;
    lastRefreshRef.current = refreshToken;
    if (initiallyExpanded) setExpanded(true);
    if (expanded || initiallyExpanded) void load();
  }, [expanded, initiallyExpanded, load, refreshToken]);

  const toggle = () => {
    setExpanded((current) => !current);
    if (!expanded && children.length === 0) void load();
  };

  const manageable =
    Boolean(canManageEntry?.(entry.path)) &&
    Boolean(onRenameEntry) &&
    Boolean(onDeleteEntry);
  const selectable = manageable && selectionMode && Boolean(onToggleSelection);
  const directoryRow = (
    <button
      type="button"
      className={`${styles.treeRow} ${
        selectedPaths?.has(entry.path) ? styles.treeRowSelected : ""
      }`}
      style={{ paddingInlineStart: 12 + depth * 16 }}
      onClick={selectable ? () => onToggleSelection?.(entry) : toggle}
      aria-expanded={expanded}
    >
      <span
        className={styles.treeChevron}
        onClick={
          selectable
            ? (event) => {
                event.stopPropagation();
                toggle();
              }
            : undefined
        }
      >
        {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
      </span>
      {expanded ? <FolderOpen size={15} /> : <Folder size={15} />}
      <span>{entry.name}</span>
      {loading && <LoaderCircle className={styles.spin} size={13} />}
    </button>
  );

  return (
    <>
      {manageable && onRenameEntry && onDeleteEntry ? (
        <div
          className={`${styles.managedRow} ${
            selectedPaths?.has(entry.path) ? styles.managedRowSelected : ""
          }`}
        >
          {selectable && (
            <Checkbox
              className={styles.entryCheckbox}
              checked={selectedPaths?.has(entry.path) ?? false}
              onClick={(event) => event.stopPropagation()}
              onChange={() => onToggleSelection?.(entry)}
              aria-label={t("files.selectEntry", { name: entry.name })}
            />
          )}
          {directoryRow}
          <EntryActions
            entry={entry}
            onRename={onRenameEntry}
            onDelete={onDeleteEntry}
          />
        </div>
      ) : (
        directoryRow
      )}
      {expanded &&
        children.map((child) => {
          if (child.kind === "directory") {
            return (
              <DirectoryNode
                key={child.path}
                entry={child}
                chatId={chatId}
                projectDirOverride={projectDirOverride}
                depth={depth + 1}
                selectedPath={selectedPath}
                onSelect={onSelect}
                root={root}
                refreshToken={refreshToken}
                canManageEntry={canManageEntry}
                onRenameEntry={onRenameEntry}
                onDeleteEntry={onDeleteEntry}
                selectionMode={selectionMode}
                selectedPaths={selectedPaths}
                onToggleSelection={onToggleSelection}
              />
            );
          }
          const manageChild =
            Boolean(canManageEntry?.(child.path)) &&
            Boolean(onRenameEntry) &&
            Boolean(onDeleteEntry);
          const childSelected = selectedPaths?.has(child.path) ?? false;
          const selectChild =
            manageChild && selectionMode && Boolean(onToggleSelection);
          const fileRow = (
            <button
              type="button"
              key={child.path}
              className={`${styles.treeRow} ${
                childSelected || child.path === selectedPath
                  ? styles.treeRowSelected
                  : ""
              }`}
              style={{ paddingInlineStart: 29 + (depth + 1) * 16 }}
              onClick={
                selectChild
                  ? () => onToggleSelection?.(child)
                  : () =>
                      onSelect({
                        source: "workspace",
                        path: child.path,
                        root,
                      })
              }
            >
              <FileGlyph name={child.name} />
              <span>{child.name}</span>
            </button>
          );
          if (!manageChild || !onRenameEntry || !onDeleteEntry) {
            return fileRow;
          }
          return (
            <div
              className={`${styles.managedRow} ${
                childSelected ? styles.managedRowSelected : ""
              }`}
              key={child.path}
            >
              {selectChild && (
                <Checkbox
                  className={styles.entryCheckbox}
                  checked={childSelected}
                  onClick={(event) => event.stopPropagation()}
                  onChange={() => onToggleSelection?.(child)}
                  aria-label={t("files.selectEntry", { name: child.name })}
                />
              )}
              {fileRow}
              <EntryActions
                entry={child}
                onRename={onRenameEntry}
                onDelete={onDeleteEntry}
              />
            </div>
          );
        })}
      {expanded && hasMore && (
        <button
          type="button"
          className={styles.loadMore}
          onClick={() => void load(cursor ?? undefined)}
          disabled={loading}
        >
          {t("files.loadMore")}
        </button>
      )}
    </>
  );
}

function MemoryDirectoryNode({
  entry,
  selectedPath,
  onSelect,
  depth,
  source,
  activeGraphRoot,
  onShowGraph,
}: {
  entry: MemoryTreeEntry;
  selectedPath: string;
  onSelect: (target: FileTarget) => void;
  depth: number;
  source: "daily" | "digest";
  activeGraphRoot: MemoryGraphRoot | null;
  onShowGraph: (root: MemoryGraphRoot) => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const graphRoot =
    source === "digest" &&
    depth === 0 &&
    (["wiki", "procedure", "personal"] as string[]).includes(entry.name)
      ? (entry.name as MemoryGraphRoot)
      : null;

  return (
    <>
      <div
        className={`${styles.memoryDirectoryRow} ${
          graphRoot && graphRoot === activeGraphRoot
            ? styles.memoryDirectoryGraphActive
            : ""
        }`}
      >
        <button
          type="button"
          className={styles.treeRow}
          style={{ paddingInlineStart: 12 + depth * 16 }}
          onClick={() => setExpanded((current) => !current)}
          aria-expanded={expanded}
        >
          {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          {expanded ? <FolderOpen size={15} /> : <Folder size={15} />}
          <span>{entry.name}</span>
        </button>
        {graphRoot && (
          <button
            type="button"
            className={styles.memoryDirectoryGraphButton}
            onClick={() => onShowGraph(graphRoot)}
            aria-label={`${t("files.memoryGraph")} · ${entry.name}`}
            title={`${t("files.memoryGraph")} · ${entry.name}`}
            aria-pressed={graphRoot === activeGraphRoot}
          >
            <Network size={14} />
            <span>{t("files.memoryGraphShort")}</span>
          </button>
        )}
      </div>
      {expanded &&
        entry.children?.map((child) =>
          child.kind === "directory" ? (
            <MemoryDirectoryNode
              key={child.path}
              entry={child}
              selectedPath={selectedPath}
              onSelect={onSelect}
              depth={depth + 1}
              source={source}
              activeGraphRoot={activeGraphRoot}
              onShowGraph={onShowGraph}
            />
          ) : (
            <button
              type="button"
              key={child.path}
              className={`${styles.treeRow} ${
                child.path === selectedPath ? styles.treeRowSelected : ""
              }`}
              style={{ paddingInlineStart: 29 + (depth + 1) * 16 }}
              onClick={() =>
                onSelect({
                  source,
                  path: child.path,
                })
              }
            >
              <FileGlyph name={child.name} />
              <span>{child.name}</span>
            </button>
          ),
        )}
    </>
  );
}

interface FilesNavigatorProps {
  selectedPath: string;
  onSelect: (target: FileTarget) => void;
  activeMemoryGraphRoot: MemoryGraphRoot | null;
  onShowMemoryGraph: (root: MemoryGraphRoot) => void;
  onShowFiles: () => void;
  scope: FilesWorkspaceScope;
}

export default function FilesNavigator({
  selectedPath,
  onSelect,
  activeMemoryGraphRoot,
  onShowMemoryGraph,
  onShowFiles,
  scope,
}: FilesNavigatorProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const chatId = scope.kind === "session" ? scope.chatId : undefined;
  const initialProjectDirOverride =
    scope.kind === "session" ? scope.projectDirOverride : undefined;
  const [pendingProjectDir, setPendingProjectDir] = useState(
    initialProjectDirOverride,
  );
  const projectDirOverride =
    scope.kind === "session" && !scope.chatId
      ? pendingProjectDir
      : initialProjectDirOverride;
  const scopeKey = filesWorkspaceScopeKey(scope);
  const [entries, setEntries] = useState<DirectoryEntry[]>([]);
  const [profileFiles, setProfileFiles] = useState<DirectoryEntry[]>([]);
  const [dailyFiles, setDailyFiles] = useState<MemoryTreeEntry[]>([]);
  const [digestFiles, setDigestFiles] = useState<MemoryTreeEntry[]>([]);
  const [enabledFiles, setEnabledFiles] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<{
    name: string;
    percent: number;
  } | null>(null);
  const [expandUpload, setExpandUpload] = useState(false);
  const [treeRefresh, setTreeRefresh] = useState(0);
  const [renameTarget, setRenameTarget] = useState<DirectoryEntry | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedPaths, setSelectedPaths] = useState<Set<string>>(new Set());
  const [source, setSource] = useState<NavigatorSource>("workspace");
  const [projectDirectory, setProjectDirectory] = useState("");
  const [workspaceDirectory, setWorkspaceDirectory] = useState("");
  const [workspaceRoot, setWorkspaceRoot] = useState<WorkspaceRoot>("project");
  const uploadRef = useRef<HTMLInputElement>(null);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );

  useEffect(() => {
    setPendingProjectDir(initialProjectDirOverride);
  }, [initialProjectDirOverride, scopeKey]);

  useEffect(() => {
    if (source !== "workspace" || workspaceRoot !== "workspace") {
      setSelectionMode(false);
    }
  }, [source, workspaceRoot]);

  useEffect(() => {
    if (!selectionMode) {
      setSelectedPaths((current) => (current.size ? new Set() : current));
    }
  }, [selectionMode]);

  const confirmDirectoryChange = useCallback(async () => {
    const state = useCodingTabsStore.getState();
    const tabs = state.tabsByAgent[scopeKey] ?? [];
    const diffs = state.diffsByAgent[scopeKey] ?? {};
    const hasUnsavedProjectState = tabs.some(
      (tab) =>
        (tab.workspaceRoot ?? "project") === "project" &&
        (tab.dirty || Boolean(diffs[tab.path])),
    );
    if (!hasUnsavedProjectState) return true;
    return new Promise<boolean>((resolve) => {
      Modal.confirm({
        title: t("files.changeDirectoryTitle"),
        content: t("files.changeDirectoryWarning"),
        okText: t("common.confirm"),
        cancelText: t("common.cancel"),
        onOk: () => resolve(true),
        onCancel: () => resolve(false),
      });
    });
  }, [scopeKey, t]);

  const handleDirectoryChanged = useCallback(() => {
    useCodingTabsStore.getState().clearProjectTabs(scopeKey);
    if (scope.kind === "session" && !scope.chatId) {
      setPendingProjectDir(
        getPendingProjectDirectory(scope.agentId, scope.sessionId) ?? undefined,
      );
    }
  }, [scope, scopeKey]);

  const sameDirectory = useMemo(
    () =>
      directoriesMatch(projectDirectory, workspaceDirectory) &&
      Boolean(workspaceDirectory),
    [projectDirectory, workspaceDirectory],
  );
  const roots = useMemo(() => workspaceRoots(sameDirectory), [sameDirectory]);
  const managedProfileNames = useMemo(
    () => new Set(profileFiles.map((file) => file.path)),
    [profileFiles],
  );

  const loadDirectoryIdentity = useCallback(async () => {
    const agentInfo = await projectDirectoryApi.get();
    const effectiveProject = projectDirOverride
      ? projectDirOverride
      : chatId
      ? (await chatProjectDirectoryApi.get(chatId)).project_dir
      : agentInfo.path;
    setProjectDirectory(effectiveProject);
    setWorkspaceDirectory(agentInfo.workspace_dir ?? agentInfo.path);
  }, [chatId, projectDirOverride]);

  const loadRoot = useCallback(
    async (root: WorkspaceRoot = workspaceRoot) => {
      setLoading(true);
      try {
        const page = await workspaceApi.listDirectory(
          "",
          undefined,
          200,
          chatId,
          root,
          projectDirOverride,
        );
        setEntries(page.entries);
        setCursor(page.next_cursor);
        setHasMore(page.has_more);
      } finally {
        setLoading(false);
      }
    },
    [chatId, projectDirOverride, workspaceRoot],
  );

  const loadProfile = useCallback(async () => {
    setLoading(true);
    try {
      const [files, enabled] = await Promise.all([
        workspaceApi.listFiles(),
        workspaceApi.getSystemPromptFiles(),
      ]);
      const order = Array.isArray(enabled) ? enabled : [];
      setEnabledFiles(order);
      setProfileFiles(
        files
          .filter((file) => isDefaultWorkspaceMarkdown(file.filename))
          .map((file) => ({
            name: file.filename.split("/").pop() ?? file.filename,
            path: file.filename,
            kind: "file" as const,
            size: file.size,
            modified_at: file.modified_time,
            preview_kind: "text" as const,
          }))
          .sort((left, right) => {
            const leftIndex = order.indexOf(left.path);
            const rightIndex = order.indexOf(right.path);
            if (leftIndex >= 0 && rightIndex >= 0) {
              return leftIndex - rightIndex;
            }
            if (leftIndex >= 0) return -1;
            if (rightIndex >= 0) return 1;
            return left.name.localeCompare(right.name);
          }),
      );
    } finally {
      setLoading(false);
    }
  }, []);

  const loadMemory = useCallback(async (section: "daily" | "digest") => {
    setLoading(true);
    try {
      const files = await workspaceApi.listMemoryFiles(section);
      const entries = files.map((file) => ({
        name: file.filename.split("/").pop() ?? file.filename,
        path: file.filename,
        kind: "file" as const,
        size: file.size,
        modified_at: file.modified_time,
        preview_kind: "text" as const,
      }));
      const tree =
        section === "daily"
          ? buildDailyMemoryTree(entries)
          : buildMemoryTree(entries);
      if (section === "daily") setDailyFiles(tree);
      else setDigestFiles(tree);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void Promise.all([loadDirectoryIdentity(), loadRoot(), loadProfile()]);
  }, [loadDirectoryIdentity, loadProfile, loadRoot]);

  useEffect(() => {
    if (sameDirectory) setWorkspaceRoot("workspace");
  }, [sameDirectory]);

  useEffect(() => {
    if (source === "profile") void loadProfile();
    if (source === "daily" || source === "digest") void loadMemory(source);
  }, [loadMemory, loadProfile, source]);

  const refreshCurrent = async () => {
    if (source === "daily" || source === "digest") {
      await loadMemory(source);
      return;
    }
    if (source === "profile") {
      await loadProfile();
      return;
    }
    await loadRoot();
  };

  const pollExtractions = useCallback(
    async (jobIds: string[]) => {
      for (let attempt = 0; attempt < 240; attempt += 1) {
        const jobs = await Promise.all(
          jobIds.map((jobId) =>
            workspaceApi
              .getInboxJob(jobId, chatId, projectDirOverride)
              .catch(() => null),
          ),
        );
        const settled =
          jobs.length > 0 &&
          jobs.every(
            (job) =>
              job !== null &&
              (job.status === "ready" || job.status === "failed"),
          );
        if (!settled) {
          await new Promise((resolve) => window.setTimeout(resolve, 1500));
          continue;
        }
        await loadRoot("workspace");
        setTreeRefresh((current) => current + 1);
        const failed = jobs.find((job) => job?.status === "failed");
        if (failed) {
          message.warning(failed.error || t("files.extractFailed"));
          return;
        }
        const count = jobs.reduce((sum, job) => sum + (job?.files ?? 0), 0);
        message.success(t("files.extractReady", { count }));
        return;
      }
      message.info(t("files.extractTimeout"));
    },
    [chatId, loadRoot, message, projectDirOverride, t],
  );

  const runUpload = async (files: File[]) => {
    const name = files.map((file) => file.name).join("、");
    setUploading(true);
    setUploadProgress({ name, percent: 0 });
    try {
      const result = await workspaceApi.uploadInbox(files, {
        chatId,
        projectDirOverride,
        onProgress: (loaded, total) =>
          setUploadProgress({
            name,
            percent:
              total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0,
          }),
      });
      setWorkspaceRoot("workspace");
      setSource("workspace");
      setExpandUpload(true);
      onShowFiles();
      await Promise.all([loadRoot("workspace"), loadProfile()]);
      // Remount the upload folder so a second upload is visible even when the
      // node was already expanded with children from the previous one.
      setTreeRefresh((current) => current + 1);
      if (result.errors?.length) {
        // Some files landed before a sibling failed; report the reason but keep
        // the successfully uploaded ones visible.
        message.warning(result.errors[0].error || t("files.uploadFailed"));
      }
      const jobIds = result.files
        .map((file) => file.job_id)
        .filter((jobId): jobId is string => Boolean(jobId));
      if (jobIds.length === 0) {
        message.success(t("files.uploadSuccess"));
        return;
      }
      message.info(t("files.extractingHint"));
      void pollExtractions(jobIds);
    } catch (error) {
      message.error(
        error instanceof Error && error.message
          ? error.message
          : t("files.uploadFailed"),
      );
    } finally {
      setUploading(false);
      setUploadProgress(null);
    }
  };

  const canManageEntry = useCallback(
    (path: string) =>
      source === "workspace" &&
      workspaceRoot === "workspace" &&
      path.startsWith("upload/") &&
      !path.split("/").includes(".jobs"),
    [source, workspaceRoot],
  );

  const closeTabsUnder = useCallback(
    (path: string) => {
      const state = useCodingTabsStore.getState();
      const tabs = state.tabsByAgent[scopeKey] ?? [];
      for (const tab of tabs) {
        const display = tab.displayPath ?? tab.path;
        if (
          tab.workspaceRoot === "workspace" &&
          (display === path || display.startsWith(`${path}/`))
        ) {
          state.closeTab(scopeKey, tab.path);
        }
      }
    },
    [scopeKey],
  );

  const requestRenameEntry = useCallback((entry: DirectoryEntry) => {
    setRenameTarget(entry);
    setRenameValue(entry.name);
  }, []);

  const submitRenameEntry = useCallback(async () => {
    if (!renameTarget) return;
    const next = renameValue.trim();
    if (!next || next === renameTarget.name) {
      setRenameTarget(null);
      return;
    }
    setRenaming(true);
    try {
      const result = await workspaceApi.renameInboxEntry(
        renameTarget.path,
        next,
        chatId,
        projectDirOverride,
      );
      const oldPath = renameTarget.path;
      closeTabsUnder(oldPath);
      if (selectedPath === oldPath || selectedPath.startsWith(`${oldPath}/`)) {
        onSelect({
          source: "workspace",
          path: `${result.path}${selectedPath.slice(oldPath.length)}`,
          root: "workspace",
        });
      }
      setRenameTarget(null);
      setSelectedPaths(new Set());
      setTreeRefresh((current) => current + 1);
      message.success(t("files.renameSuccess"));
    } catch (error) {
      message.error(
        error instanceof Error && error.message
          ? error.message
          : t("files.renameFailed"),
      );
    } finally {
      setRenaming(false);
    }
  }, [
    chatId,
    closeTabsUnder,
    message,
    onSelect,
    projectDirOverride,
    renameTarget,
    renameValue,
    selectedPath,
    t,
  ]);

  const toggleSelectedEntry = useCallback((entry: DirectoryEntry) => {
    setSelectedPaths((current) => {
      const next = new Set(current);
      if (next.has(entry.path)) next.delete(entry.path);
      else next.add(entry.path);
      return next;
    });
  }, []);

  const requestDeleteEntry = useCallback(
    (entry: DirectoryEntry) => {
      const isDirectory = entry.kind === "directory";
      Modal.confirm({
        title: t("files.deleteConfirmTitle"),
        content: t(
          isDirectory
            ? "files.deleteDirectoryWarning"
            : "files.deleteFileWarning",
          { name: entry.name },
        ),
        okText: t("files.delete"),
        okButtonProps: { danger: true },
        cancelText: t("common.cancel"),
        onOk: async () => {
          try {
            const result = await workspaceApi.deleteInboxEntries(
              [entry.path],
              isDirectory,
              chatId,
              projectDirOverride,
            );
            if (result.deleted.length) {
              closeTabsUnder(entry.path);
            }
            setSelectedPaths(new Set());
            setTreeRefresh((current) => current + 1);
            if (result.failed.length) {
              message.warning(
                result.failed[0].error || t("files.deleteFailed"),
              );
            } else {
              message.success(
                t("files.deleteSelectedSuccess", {
                  count: result.deleted.length,
                  size: formatBytes(result.freed_bytes),
                }),
              );
            }
          } catch (error) {
            message.error(
              error instanceof Error && error.message
                ? error.message
                : t("files.deleteFailed"),
            );
            throw error;
          }
        },
      });
    },
    [chatId, closeTabsUnder, message, projectDirOverride, t],
  );

  const selectAllInUpload = useCallback(async () => {
    try {
      const paths: string[] = [];
      let cursor: string | undefined;
      for (let pageIndex = 0; pageIndex < 500; pageIndex += 1) {
        const page = await workspaceApi.listDirectory(
          "upload",
          cursor,
          200,
          chatId,
          "workspace",
          projectDirOverride,
        );
        for (const entry of page.entries) {
          if (!entry.path.split("/").includes(".jobs")) {
            paths.push(entry.path);
          }
        }
        if (!page.has_more || !page.next_cursor) break;
        cursor = page.next_cursor;
      }
      setSelectedPaths(new Set(paths));
    } catch (error) {
      message.error(
        error instanceof Error && error.message
          ? error.message
          : t("files.deleteFailed"),
      );
    }
  }, [chatId, message, projectDirOverride, t]);

  const requestDeleteSelected = useCallback(() => {
    const paths = Array.from(selectedPaths);
    if (!paths.length) return;
    Modal.confirm({
      title: t("files.deleteConfirmTitle"),
      content: t("files.deleteSelectedWarning", { count: paths.length }),
      okText: t("files.delete"),
      okButtonProps: { danger: true },
      cancelText: t("common.cancel"),
      onOk: async () => {
        try {
          const result = await workspaceApi.deleteInboxEntries(
            paths,
            true,
            chatId,
            projectDirOverride,
          );
          for (const path of result.deleted) closeTabsUnder(path);
          setSelectedPaths(new Set());
          setTreeRefresh((current) => current + 1);
          if (result.deleted.length) {
            message.success(
              t("files.deleteSelectedSuccess", {
                count: result.deleted.length,
                size: formatBytes(result.freed_bytes),
              }),
            );
          }
          if (result.failed.length) {
            message.warning(result.failed[0].error || t("files.deleteFailed"));
          }
        } catch (error) {
          message.error(
            error instanceof Error && error.message
              ? error.message
              : t("files.deleteFailed"),
          );
          throw error;
        }
      },
    });
  }, [chatId, closeTabsUnder, message, projectDirOverride, selectedPaths, t]);

  const toggleProfileFile = async (filename: string) => {
    const next = enabledFiles.includes(filename)
      ? enabledFiles.filter((file) => file !== filename)
      : [...enabledFiles, filename];
    await workspaceApi.setSystemPromptFiles(next);
    setEnabledFiles(next);
  };

  const reorderProfileFiles = async (event: DragEndEvent) => {
    if (!event.over || event.active.id === event.over.id) return;
    const oldIndex = enabledFiles.indexOf(String(event.active.id));
    const newIndex = enabledFiles.indexOf(String(event.over.id));
    if (oldIndex < 0 || newIndex < 0) return;
    const next = arrayMove(enabledFiles, oldIndex, newIndex);
    await workspaceApi.setSystemPromptFiles(next);
    setEnabledFiles(next);
    setProfileFiles((current) =>
      [...current].sort((left, right) => {
        const leftIndex = next.indexOf(left.path);
        const rightIndex = next.indexOf(right.path);
        if (leftIndex >= 0 && rightIndex >= 0) return leftIndex - rightIndex;
        if (leftIndex >= 0) return -1;
        if (rightIndex >= 0) return 1;
        return left.name.localeCompare(right.name);
      }),
    );
  };

  const displayEntries = useMemo(() => {
    if (source === "daily") return dailyFiles;
    if (source === "digest") return digestFiles;
    if (source === "profile") return profileFiles;
    if (source === "workspace") return entries;
    return [];
  }, [dailyFiles, digestFiles, entries, profileFiles, source]);

  return (
    <aside
      className={styles.navigator}
      data-source={source}
      data-root={workspaceRoot}
      aria-label={t("files.navigator")}
    >
      <header className={styles.navigatorHeader}>
        <div className={styles.directoryToolbar}>
          <div className={styles.directoryContext} data-root={workspaceRoot}>
            <span className={styles.directoryContextIcon}>
              {workspaceRoot === "project" ? (
                <FolderOpen size={15} />
              ) : (
                <Settings2 size={15} />
              )}
            </span>
            <div className={styles.directoryContextBody}>
              <span className={styles.directoryContextLabel}>
                {t(`files.${workspaceRoot}Directory`)}
              </span>
              {workspaceRoot === "project" ? (
                <SessionProjectDirectory
                  scope={scope}
                  showFullPath
                  beforeChange={confirmDirectoryChange}
                  onChanged={handleDirectoryChanged}
                />
              ) : (
                <span className={styles.directoryIdentity}>
                  <span className={styles.directoryIdentityText}>
                    <strong>
                      {workspaceDirectory
                        .replace(/[\\/]+$/, "")
                        .split(/[\\/]/)
                        .pop() || t("files.workspaceDirectory")}
                    </strong>
                    <span title={workspaceDirectory}>{workspaceDirectory}</span>
                  </span>
                </span>
              )}
            </div>
            {roots.length > 1 && (
              <button
                type="button"
                className={styles.directorySwitch}
                onClick={() =>
                  setWorkspaceRoot((current) =>
                    current === "project" ? "workspace" : "project",
                  )
                }
                aria-label={t("files.switchDirectory")}
                title={t("files.switchDirectory")}
              >
                <ArrowLeftRight size={14} />
              </button>
            )}
          </div>
          <div className={styles.directoryTools}>
            {source === "workspace" && workspaceRoot === "workspace" && (
              <button
                type="button"
                className={styles.iconButton}
                data-active={selectionMode || undefined}
                onClick={() => setSelectionMode((current) => !current)}
                aria-label={t("files.selectFiles")}
                title={t("files.selectFiles")}
              >
                <ListChecks size={15} />
              </button>
            )}
            <button
              type="button"
              className={styles.iconButton}
              onClick={() => void refreshCurrent()}
              aria-label={t("common.refresh")}
            >
              <RefreshCw size={15} />
            </button>
            <button
              type="button"
              className={styles.iconButton}
              onClick={() => uploadRef.current?.click()}
              aria-label={t("files.upload")}
              title={t("files.uploadHint")}
              disabled={uploading}
            >
              {uploading ? (
                <LoaderCircle className={styles.spin} size={15} />
              ) : (
                <Upload size={15} />
              )}
            </button>
          </div>
        </div>
        <input
          ref={uploadRef}
          type="file"
          multiple
          hidden
          onChange={(event) => {
            const files = Array.from(event.target.files ?? []);
            event.target.value = "";
            if (files.length > 0) void runUpload(files);
          }}
        />
      </header>
      <div className={styles.sourceTabs} role="tablist">
        {(["workspace", "profile", "daily", "digest"] as NavigatorSource[]).map(
          (item) => (
            <button
              type="button"
              role="tab"
              aria-selected={source === item}
              key={item}
              className={`${styles.sourceTab} ${
                source === item ? styles.sourceTabActive : ""
              }`}
              data-source={item}
              onClick={() => {
                setSource(item);
                onShowFiles();
              }}
            >
              {t(`files.${item}`)}
            </button>
          ),
        )}
      </div>
      {uploading && uploadProgress && (
        <div className={styles.uploadProgress}>
          <span
            className={styles.uploadProgressLabel}
            title={uploadProgress.name}
          >
            {uploadProgress.name}
          </span>
          <Progress
            percent={uploadProgress.percent}
            size="small"
            showInfo={false}
            status="active"
          />
        </div>
      )}
      {selectionMode && (
        <div className={styles.selectionBar}>
          <span>{t("files.selectedCount", { count: selectedPaths.size })}</span>
          <div className={styles.selectionActions}>
            <button
              type="button"
              className={styles.selectionCancel}
              onClick={() => void selectAllInUpload()}
            >
              {t("files.selectAll")}
            </button>
            <button
              type="button"
              className={styles.selectionDelete}
              disabled={selectedPaths.size === 0}
              onClick={requestDeleteSelected}
            >
              <Trash2 size={13} />
              {t("files.deleteSelected")}
            </button>
            <button
              type="button"
              className={styles.selectionCancel}
              onClick={() => setSelectionMode(false)}
            >
              {t("common.cancel")}
            </button>
          </div>
        </div>
      )}
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={(event) => void reorderProfileFiles(event)}
      >
        <SortableContext
          items={enabledFiles}
          strategy={verticalListSortingStrategy}
        >
          <div className={styles.tree} role="tree" aria-busy={loading}>
            {loading && displayEntries.length === 0 ? (
              <div className={styles.empty}>
                <LoaderCircle className={styles.spin} size={16} />
                {t("common.loading")}
              </div>
            ) : (
              displayEntries.map((entry) => {
                if (entry.kind === "directory") {
                  if (source === "daily" || source === "digest") {
                    return (
                      <MemoryDirectoryNode
                        key={entry.path}
                        entry={entry}
                        selectedPath={selectedPath}
                        onSelect={onSelect}
                        depth={0}
                        source={source}
                        activeGraphRoot={activeMemoryGraphRoot}
                        onShowGraph={onShowMemoryGraph}
                      />
                    );
                  }
                  return (
                    <DirectoryNode
                      key={entry.path}
                      entry={entry}
                      chatId={chatId}
                      projectDirOverride={projectDirOverride}
                      depth={0}
                      selectedPath={selectedPath}
                      onSelect={onSelect}
                      root={workspaceRoot}
                      initiallyExpanded={
                        source === "workspace" &&
                        expandUpload &&
                        (entry.path === "upload" || entry.name === "upload")
                      }
                      refreshToken={treeRefresh}
                      canManageEntry={canManageEntry}
                      onRenameEntry={requestRenameEntry}
                      onDeleteEntry={requestDeleteEntry}
                      selectionMode={selectionMode}
                      selectedPaths={selectedPaths}
                      onToggleSelection={toggleSelectedEntry}
                    />
                  );
                }
                const isProfileFile =
                  source === "profile" && managedProfileNames.has(entry.path);
                if (isProfileFile) {
                  return (
                    <ProfileFileRow
                      key={entry.path}
                      entry={entry}
                      enabled={enabledFiles.includes(entry.path)}
                      selected={entry.path === selectedPath}
                      onSelect={() =>
                        onSelect({ source: "profile", path: entry.path })
                      }
                      onToggle={() => void toggleProfileFile(entry.path)}
                    />
                  );
                }
                return (
                  <button
                    type="button"
                    key={entry.path}
                    className={`${styles.treeRow} ${
                      entry.path === selectedPath ? styles.treeRowSelected : ""
                    }`}
                    onClick={() =>
                      onSelect({
                        source,
                        path: entry.path,
                        root:
                          source === "workspace" ? workspaceRoot : undefined,
                      })
                    }
                  >
                    <FileGlyph name={entry.name} />
                    <span>{entry.name}</span>
                  </button>
                );
              })
            )}
            {!loading && displayEntries.length === 0 && (
              <div className={styles.empty}>{t("files.sourceEmpty")}</div>
            )}
            {source === "workspace" && hasMore && (
              <button
                type="button"
                className={styles.loadMore}
                onClick={async () => {
                  const page = await workspaceApi.listDirectory(
                    "",
                    cursor ?? undefined,
                    200,
                    chatId,
                    workspaceRoot,
                    projectDirOverride,
                  );
                  setEntries((current) => [...current, ...page.entries]);
                  setCursor(page.next_cursor);
                  setHasMore(page.has_more);
                }}
              >
                {t("files.loadMore")}
              </button>
            )}
          </div>
        </SortableContext>
      </DndContext>
      <Modal
        open={renameTarget !== null}
        title={t("files.renameTitle")}
        okText={t("common.confirm")}
        cancelText={t("common.cancel")}
        confirmLoading={renaming}
        okButtonProps={{
          disabled:
            !renameValue.trim() || renameValue.trim() === renameTarget?.name,
        }}
        onCancel={() => {
          if (!renaming) setRenameTarget(null);
        }}
        onOk={() => void submitRenameEntry()}
      >
        <p className={styles.renameDescription}>
          {t("files.renameDescription")}
        </p>
        <Input
          autoFocus
          value={renameValue}
          maxLength={200}
          onChange={(event) => setRenameValue(event.target.value)}
          onFocus={(event) => event.currentTarget.select()}
          onPressEnter={() => {
            if (!renaming) void submitRenameEntry();
          }}
        />
      </Modal>
    </aside>
  );
}
