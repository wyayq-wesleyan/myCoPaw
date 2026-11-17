import { useEffect, useMemo, useRef, useState } from "react";
import type { Key, ReactNode } from "react";
import {
  DeleteOutlined,
  FileExcelOutlined,
  FileImageOutlined,
  FilePdfOutlined,
  FileTextOutlined,
  FileWordOutlined,
  FolderOpenOutlined,
  FolderOutlined,
  MoreOutlined,
  SwapOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import {
  Button,
  Checkbox,
  Dropdown,
  Input,
  Modal,
  Popconfirm,
  Tooltip,
  Tree,
  message,
} from "antd";
import type { DataNode } from "antd/es/tree";

import { FilePreviewDrawer } from "./FilePreviewDrawer";

interface CaseFileTreeProps {
  caseCode: string;
  rootName?: string;
  materialName?: string;
  spaceLabel?: string;
  mode?: "case" | "office";
}

interface FileNode {
  id: string;
  name: string;
  type: "folder" | "file";
  source?: "case" | "upload" | "tmp" | "res";
  size?: string;
  updatedAt?: string;
  children?: FileNode[];
}

/** tmp / res 是智能体固定目录：只读结构，不能删除、不能改名、不能移出。 */
const PROTECTED_FOLDER_NAMES = new Set(["tmp", "res"]);

function isProtectedFolder(node: FileNode): boolean {
  return node.type === "folder" && PROTECTED_FOLDER_NAMES.has(node.name);
}

function fileIcon(name: string) {
  const ext = name.split(".").pop()?.toLowerCase();
  if (ext === "pdf") return <FilePdfOutlined />;
  if (ext === "doc" || ext === "docx") return <FileWordOutlined />;
  if (ext === "xls" || ext === "xlsx" || ext === "csv") {
    return <FileExcelOutlined />;
  }
  if (ext === "png" || ext === "jpg" || ext === "jpeg") {
    return <FileImageOutlined />;
  }
  return <FileTextOutlined />;
}

function createTree(
  caseCode: string,
  rootName?: string,
  materialName?: string,
  mode: "case" | "office" = "case",
): FileNode[] {
  const materialFiles: FileNode[] =
    mode === "office"
      ? [
          {
            id: "case-file-1",
            name: "周例会会议纪要.docx",
            type: "file",
            source: "case",
            size: "126 KB",
            updatedAt: "今天",
          },
          {
            id: "case-file-2",
            name: "平台接入情况汇报.pptx",
            type: "file",
            source: "case",
            size: "2.4 MB",
            updatedAt: "昨天",
          },
        ]
      : [
          {
            id: "case-file-1",
            name: "受案登记表.pdf",
            type: "file",
            source: "case",
            size: "1.2 MB",
            updatedAt: "11-12",
          },
          {
            id: "case-file-2",
            name: "询问笔录.docx",
            type: "file",
            source: "case",
            size: "386 KB",
            updatedAt: "11-13",
          },
        ];
  const uploadFiles: FileNode[] =
    mode === "office"
      ? [
          {
            id: "upload-1",
            name: "督办事项清单.xlsx",
            type: "file",
            source: "upload",
            size: "28 KB",
            updatedAt: "今天",
          },
        ]
      : [
          {
            id: "upload-1",
            name: "线索截图.png",
            type: "file",
            source: "upload",
            size: "842 KB",
            updatedAt: "今天",
          },
        ];
  const tmpFiles: FileNode[] =
    mode === "office"
      ? [
          {
            id: "tmp-1",
            name: "会议转写_20261116.txt",
            type: "file",
            source: "tmp",
            size: "32 KB",
            updatedAt: "今天",
          },
          {
            id: "tmp-2",
            name: "纪要草稿.md",
            type: "file",
            source: "tmp",
            size: "6 KB",
            updatedAt: "今天",
          },
        ]
      : [
          {
            id: "tmp-1",
            name: "号码解析_20261116.json",
            type: "file",
            source: "tmp",
            size: "18 KB",
            updatedAt: "今天",
          },
          {
            id: "tmp-2",
            name: "回执识别.txt",
            type: "file",
            source: "tmp",
            size: "4 KB",
            updatedAt: "今天",
          },
        ];
  const resFiles: FileNode[] =
    mode === "office"
      ? [
          {
            id: "res-1",
            name: "督办清单_20261116.xlsx",
            type: "file",
            source: "res",
            size: "36 KB",
            updatedAt: "今天",
          },
          {
            id: "res-2",
            name: "会议纪要_20261116.docx",
            type: "file",
            source: "res",
            size: "88 KB",
            updatedAt: "今天",
          },
        ]
      : [
          {
            id: "res-1",
            name: "核查结果_陈某某.xlsx",
            type: "file",
            source: "res",
            size: "46 KB",
            updatedAt: "今天",
          },
          {
            id: "res-2",
            name: "核查报告_20261116.pdf",
            type: "file",
            source: "res",
            size: "1.8 MB",
            updatedAt: "今天",
          },
        ];
  return [
    {
      id: "case-root",
      name: rootName ?? `案件 ${caseCode}`,
      type: "folder",
      source: "case",
      children: [
        {
          id: "case-files",
          name: materialName ?? "案件材料",
          type: "folder",
          source: "case",
          children: materialFiles,
        },
        {
          id: "uploads",
          name: "上传文件",
          type: "folder",
          source: "upload",
          children: uploadFiles,
        },
        {
          id: "tmp",
          name: "tmp",
          type: "folder",
          source: "tmp",
          children: tmpFiles,
        },
        {
          id: "res",
          name: "res",
          type: "folder",
          source: "res",
          children: resFiles,
        },
      ],
    },
  ];
}

function findNode(nodes: FileNode[], id: string): FileNode | null {
  for (const node of nodes) {
    if (node.id === id) return node;
    const hit = node.children ? findNode(node.children, id) : null;
    if (hit) return hit;
  }
  return null;
}

function findParent(
  nodes: FileNode[],
  id: string,
  parent: FileNode | null = null,
): FileNode | null {
  for (const node of nodes) {
    if (node.id === id) return parent;
    if (node.children) {
      const hit = findParent(node.children, id, node);
      if (hit || node.children.some((child) => child.id === id)) {
        return hit ?? node;
      }
    }
  }
  return null;
}

function collectNodes(nodes: FileNode[]): FileNode[] {
  return nodes.flatMap((node) => [
    node,
    ...(node.children ? collectNodes(node.children) : []),
  ]);
}

function collectFolders(nodes: FileNode[]): FileNode[] {
  return nodes.flatMap((node) => [
    ...(node.type === "folder" ? [node] : []),
    ...(node.children ? collectFolders(node.children) : []),
  ]);
}

export function CaseFileTree({
  caseCode,
  rootName,
  materialName,
  spaceLabel = "当前案件空间",
  mode = "case",
}: CaseFileTreeProps) {
  const [tree, setTree] = useState<FileNode[]>(() =>
    createTree(caseCode, rootName, materialName, mode),
  );
  const [expandedKeys, setExpandedKeys] = useState<string[]>([
    "case-root",
    "case-files",
    "uploads",
  ]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [currentFolderId, setCurrentFolderId] = useState("uploads");
  const [previewFile, setPreviewFile] = useState<FileNode | null>(null);
  const [renameTarget, setRenameTarget] = useState<FileNode | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [moveTarget, setMoveTarget] = useState<FileNode | null>(null);
  const [moveFolderId, setMoveFolderId] = useState("");
  const [createFolderOpen, setCreateFolderOpen] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const uploadTargetRef = useRef<{ id: string; name: string } | null>(null);

  useEffect(() => {
    setTree(createTree(caseCode, rootName, materialName, mode));
    setSelectedIds([]);
    setCurrentFolderId("uploads");
  }, [caseCode, rootName, materialName, mode]);

  const allNodes = useMemo(() => collectNodes(tree), [tree]);
  const allFiles = allNodes.filter((node) => node.type === "file");
  const folders = useMemo(() => collectFolders(tree), [tree]);

  const allSelected =
    allFiles.length > 0 &&
    allFiles.every((file) => selectedIds.includes(file.id));
  const someSelected = allFiles.some((file) => selectedIds.includes(file.id));

  const currentFolder = findNode(tree, currentFolderId);
  const currentFolderName =
    currentFolder?.type === "folder" ? currentFolder.name : "上传文件";

  const toggleSelected = (fileId: string) => {
    setSelectedIds((previous) =>
      previous.includes(fileId)
        ? previous.filter((id) => id !== fileId)
        : [...previous, fileId],
    );
  };

  const toggleSelectAll = () => {
    setSelectedIds(allSelected ? [] : allFiles.map((file) => file.id));
  };

  const invertSelection = () => {
    setSelectedIds(
      allFiles
        .filter((file) => !selectedIds.includes(file.id))
        .map((file) => file.id),
    );
  };

  const removeNodes = (nodes: FileNode[], ids: string[]): FileNode[] =>
    nodes
      .filter((node) => !ids.includes(node.id))
      .map((node) =>
        node.children
          ? { ...node, children: removeNodes(node.children, ids) }
          : node,
      );

  const deleteSelected = () => {
    const blocked = selectedIds
      .map((id) => findNode(tree, id))
      .filter((node): node is FileNode => Boolean(node))
      .filter((node) => node.source === "tmp" || node.source === "res");
    const deletable = selectedIds.filter(
      (id) => !blocked.some((node) => node.id === id),
    );
    if (deletable.length > 0) {
      setTree((previous) => removeNodes(previous, deletable));
      setSelectedIds([]);
    }
    if (blocked.length > 0) {
      message.warning("tmp 和 res 是智能体固定目录，其中的文件不能手动删除");
    } else {
      message.success(`已删除 ${deletable.length} 个文件`);
    }
  };

  const insertChild = (
    nodes: FileNode[],
    folderId: string,
    child: FileNode,
  ): FileNode[] =>
    nodes.map((node) => {
      if (node.id === folderId && node.type === "folder") {
        return { ...node, children: [...(node.children ?? []), child] };
      }
      if (node.children) {
        return {
          ...node,
          children: insertChild(node.children, folderId, child),
        };
      }
      return node;
    });

  const removeNodeById = (nodes: FileNode[], id: string): FileNode[] =>
    nodes
      .filter((node) => node.id !== id)
      .map((node) =>
        node.children
          ? { ...node, children: removeNodeById(node.children, id) }
          : node,
      );

  const moveNode = (nodeId: string, targetFolderId: string) => {
    const node = findNode(tree, nodeId);
    if (!node) return;
    if (node.source === "tmp" || node.source === "res") {
      message.warning("tmp / res 中的文件不允许移动");
      return;
    }
    const target = findNode(tree, targetFolderId);
    if (!target || target.type !== "folder") return;
    if (target.id === nodeId) return;

    setTree((previous) => {
      const detached = removeNodeById(previous, nodeId);
      return insertChild(detached, targetFolderId, node);
    });
    setExpandedKeys((previous) =>
      previous.includes(targetFolderId)
        ? previous
        : [...previous, targetFolderId],
    );
    message.success(`已移动到「${target.name}」`);
    setMoveTarget(null);
    setMoveFolderId("");
  };

  const openUploadFor = (folderId: string, folderName: string) => {
    uploadTargetRef.current = { id: folderId, name: folderName };
    uploadInputRef.current?.click();
  };

  const addUploads = (
    folderId: string,
    folderName: string,
    files: FileList,
  ) => {
    const list = Array.from(files);
    if (list.length === 0) return;
    setTree((previous) =>
      list.reduce(
        (acc, file, index) =>
          insertChild(acc, folderId, {
            id: `upload-${Date.now()}-${index}-${file.name}`,
            name: file.name,
            type: "file",
            source: "upload",
            size:
              file.size > 1024 * 1024
                ? `${(file.size / 1024 / 1024).toFixed(1)} MB`
                : `${Math.max(1, Math.round(file.size / 1024))} KB`,
            updatedAt: "刚刚",
          }),
        previous,
      ),
    );
    setExpandedKeys((previous) =>
      previous.includes(folderId) ? previous : [...previous, folderId],
    );
    message.success(`已上传 ${list.length} 个文件到「${folderName}」`);
  };

  const createFolder = () => {
    const name = newFolderName.trim();
    if (!name) return;
    const parentId = currentFolderId || "case-root";
    setTree((previous) =>
      insertChild(previous, parentId, {
        id: `folder-${Date.now()}`,
        name,
        type: "folder",
        source: "case",
        children: [],
      }),
    );
    setExpandedKeys((previous) =>
      previous.includes(parentId) ? previous : [...previous, parentId],
    );
    setCreateFolderOpen(false);
    setNewFolderName("");
    message.success(`已创建目录「${name}」`);
  };

  const renameNode = () => {
    if (!renameTarget) return;
    if (renameTarget.source === "tmp" || renameTarget.source === "res") {
      message.warning("tmp / res 是固定目录，不能改名");
      return;
    }
    const name = renameValue.trim();
    if (!name) return;
    setTree((previous) =>
      previous.map((node) => {
        if (node.id === renameTarget.id) return { ...node, name };
        if (node.children) {
          const walk = (nodes: FileNode[]): FileNode[] =>
            nodes.map((item) =>
              item.id === renameTarget.id
                ? { ...item, name }
                : item.children
                ? { ...item, children: walk(item.children) }
                : item,
            );
          return { ...node, children: walk(node.children) };
        }
        return node;
      }),
    );
    setRenameTarget(null);
    message.success("已重命名");
  };

  const handleDrop = (info: {
    dragNode: { key: Key };
    node: { key: Key };
    dropToGap: boolean;
  }) => {
    const dragId = String(info.dragNode.key);
    const dropId = String(info.node.key);
    const dragged = findNode(tree, dragId);
    const dropNode = findNode(tree, dropId);
    if (!dragged || !dropNode) return;
    if (dragged.source === "tmp" || dragged.source === "res") {
      message.warning("tmp / res 中的文件不允许移动");
      return;
    }
    const targetFolderId =
      dropNode.type === "folder"
        ? dropNode.id
        : findParent(tree, dropNode.id)?.id;
    if (!targetFolderId) return;
    moveNode(dragId, targetFolderId);
  };

  const renderTitle = (node: FileNode) => {
    const isFile = node.type === "file";
    const locked = node.source === "tmp" || node.source === "res";
    const isCurrentFolder =
      node.type === "folder" && node.id === currentFolderId;
    return (
      <div
        className={`dl-tree-row ${
          isCurrentFolder ? "dl-tree-row-current" : ""
        }`}
        onClick={() => {
          if (node.type === "folder") {
            setCurrentFolderId(node.id);
            setExpandedKeys((previous) =>
              previous.includes(node.id)
                ? previous.filter((key) => key !== node.id)
                : [...previous, node.id],
            );
          }
        }}
      >
        {isFile ? (
          <Checkbox
            checked={selectedIds.includes(node.id)}
            onChange={() => toggleSelected(node.id)}
            onClick={(event) => event.stopPropagation()}
          />
        ) : (
          <span className="dl-tree-folder-icon">
            {expandedKeys.includes(node.id) ? (
              <FolderOpenOutlined />
            ) : (
              <FolderOutlined />
            )}
          </span>
        )}

        {isFile ? (
          <button
            type="button"
            className="dl-tree-name dl-tree-name-button"
            title={`预览 ${node.name}`}
            onClick={(event) => {
              event.stopPropagation();
              setPreviewFile(node);
            }}
          >
            <span className="dl-tree-type-icon">{fileIcon(node.name)}</span>
            {node.name}
          </button>
        ) : (
          <span className="dl-tree-name">
            {node.name}
            {node.source === "tmp" ? (
              <span className="dl-file-badge dl-file-badge-tmp">临时</span>
            ) : null}
            {node.source === "res" ? (
              <span className="dl-file-badge dl-file-badge-res">结果</span>
            ) : null}
          </span>
        )}

        {isFile ? (
          <>
            <span className="dl-tree-size">{node.size}</span>
            <span className="dl-tree-time">{node.updatedAt}</span>
          </>
        ) : null}

        <span className="dl-tree-actions">
          {locked ? (
            <Tooltip title="智能体固定目录，不可修改">
              <span className="dl-tree-lock">固定</span>
            </Tooltip>
          ) : (
            <Dropdown
              trigger={["click"]}
              menu={{
                items:
                  node.type === "folder"
                    ? [
                        { key: "new-folder", label: "新建子目录" },
                        { key: "upload", label: "上传到此处" },
                        { type: "divider" as const },
                        { key: "rename", label: "重命名" },
                        { type: "divider" as const },
                        { key: "delete", label: "删除该目录", danger: true },
                      ]
                    : [
                        { key: "preview", label: "预览" },
                        { key: "rename", label: "重命名" },
                        { key: "move", label: "移动到…" },
                        { type: "divider" as const },
                        { key: "delete", label: "删除", danger: true },
                      ],
                onClick: ({ key, domEvent }) => {
                  domEvent.stopPropagation();
                  if (key === "preview") setPreviewFile(node);
                  if (key === "new-folder") {
                    setCurrentFolderId(node.id);
                    setNewFolderName("");
                    setCreateFolderOpen(true);
                  }
                  if (key === "upload") {
                    setCurrentFolderId(node.id);
                    openUploadFor(node.id, node.name);
                  }
                  if (key === "rename") {
                    setRenameTarget(node);
                    setRenameValue(node.name);
                  }
                  if (key === "move") {
                    setMoveTarget(node);
                    setMoveFolderId(currentFolderId);
                  }
                  if (key === "delete") {
                    setTree((previous) => removeNodeById(previous, node.id));
                    setSelectedIds((previous) =>
                      previous.filter((id) => id !== node.id),
                    );
                    message.success(`已删除「${node.name}」`);
                  }
                },
              }}
            >
              <button
                type="button"
                className="dl-tree-more"
                onClick={(event) => event.stopPropagation()}
              >
                <MoreOutlined />
              </button>
            </Dropdown>
          )}
        </span>
      </div>
    );
  };

  const treeData: DataNode[] = useMemo(
    () => tree.map((node) => toAntTree(node, expandedKeys, renderTitle)),
    [tree, expandedKeys, selectedIds, currentFolderId],
  );

  const moveFolders = folders.filter((folder) => folder.id !== moveTarget?.id);

  return (
    <section className="dl-side-files">
      <div className="dl-tree-toolbar">
        <div className="dl-tree-toolbar-title">
          <span className="dl-side-section-title">{spaceLabel}</span>
          <span className="dl-tree-current">当前目录：{currentFolderName}</span>
        </div>
        <span className="dl-tree-hint">目录操作在每行右侧的 ⋯ 菜单中</span>
        <input
          ref={uploadInputRef}
          type="file"
          multiple
          hidden
          onChange={(event) => {
            const target = uploadTargetRef.current;
            if (target && event.target.files) {
              addUploads(target.id, target.name, event.target.files);
            }
            event.target.value = "";
            uploadTargetRef.current = null;
          }}
        />
      </div>

      <div className="dl-tree-selectbar">
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
          title={`删除 ${selectedIds.length} 个文件？`}
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
            icon={<DeleteOutlined />}
          >
            删除 {selectedIds.length > 0 ? selectedIds.length : ""}
          </Button>
        </Popconfirm>
      </div>

      <div className="dl-tree-wrap">
        <Tree
          blockNode
          showLine={{ showLeafIcon: false }}
          draggable={{
            icon: false,
            nodeDraggable: (node) => node.isLeaf !== false || true,
          }}
          treeData={treeData}
          expandedKeys={expandedKeys}
          onExpand={(keys) => setExpandedKeys(keys.map(String))}
          onDrop={(info) =>
            handleDrop(
              info as unknown as {
                dragNode: { key: Key };
                node: { key: Key };
                dropToGap: boolean;
              },
            )
          }
          className="dl-file-ant-tree"
        />
      </div>

      <div className="dl-file-legend">
        <span>
          <i className="dl-file-legend-dot dl-file-legend-upload" />
          可新建目录、重命名、拖拽移动
        </span>
        <span>
          <i className="dl-file-legend-dot dl-file-legend-tmp" />
          tmp：智能体临时文件，不可改
        </span>
        <span>
          <i className="dl-file-legend-dot dl-file-legend-res" />
          res：智能体结果文件，不可改
        </span>
      </div>

      <FilePreviewDrawer
        file={previewFile}
        onClose={() => setPreviewFile(null)}
      />

      <Modal
        title="新建目录"
        open={createFolderOpen}
        onCancel={() => setCreateFolderOpen(false)}
        onOk={createFolder}
        okText="创建"
        cancelText="取消"
        okButtonProps={{ disabled: !newFolderName.trim() }}
        width={420}
      >
        <div className="dl-tree-modal">
          <span>将在「{currentFolderName}」下创建</span>
          <Input
            autoFocus
            value={newFolderName}
            placeholder="目录名称"
            onChange={(event) => setNewFolderName(event.target.value)}
            onPressEnter={createFolder}
          />
        </div>
      </Modal>

      <Modal
        title="重命名"
        open={Boolean(renameTarget)}
        onCancel={() => setRenameTarget(null)}
        onOk={renameNode}
        okText="保存"
        cancelText="取消"
        okButtonProps={{ disabled: !renameValue.trim() }}
        width={420}
      >
        <div className="dl-tree-modal">
          <Input
            autoFocus
            value={renameValue}
            onChange={(event) => setRenameValue(event.target.value)}
            onPressEnter={renameNode}
          />
        </div>
      </Modal>

      <Modal
        title="移动到目录"
        open={Boolean(moveTarget)}
        onCancel={() => setMoveTarget(null)}
        onOk={() => {
          if (moveTarget && moveFolderId) {
            moveNode(moveTarget.id, moveFolderId);
          }
        }}
        okText="移动"
        cancelText="取消"
        okButtonProps={{ disabled: !moveFolderId }}
        width={460}
      >
        <div className="dl-tree-modal">
          <span>将「{moveTarget?.name}」移动到：</span>
          <div className="dl-tree-folder-picker">
            {moveFolders.map((folder) => (
              <button
                key={folder.id}
                type="button"
                className={
                  folder.id === moveFolderId ? "dl-tree-folder-active" : ""
                }
                onClick={() => setMoveFolderId(folder.id)}
              >
                <FolderOutlined />
                {folder.name}
                {folder.source === "tmp" || folder.source === "res" ? (
                  <em>固定</em>
                ) : null}
              </button>
            ))}
          </div>
        </div>
      </Modal>
    </section>
  );
}

function toAntTree(
  node: FileNode,
  expandedKeys: string[],
  renderTitle: (node: FileNode) => ReactNode,
): DataNode {
  return {
    key: node.id,
    isLeaf: node.type === "file",
    title: renderTitle(node),
    children: node.children?.map((child) =>
      toAntTree(child, expandedKeys, renderTitle),
    ),
  } as DataNode;
}

export default CaseFileTree;
