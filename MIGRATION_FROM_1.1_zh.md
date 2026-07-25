# 1.1 数据迁移到 2.0.1

2.0.1 使用新数据目录，不能与生产中的 1.1 容器共用挂载路径。升级必须先保留
1.1 容器和原始卷，迁移成功并验收后再切换端口。

## 保留和迁移的内容

- `working.secret/auth.json`：账号、密码哈希、禁用状态和管理员角色。
- `working/users/`：每位用户的智能体排序、默认智能体和权限状态。
- `working/workspaces/`：智能体配置、聊天、定时任务、会话、上传文件、知识库原始文档、技能和记忆。
- `working/skill_pool/`：已有默认和自定义技能。
- `working/config.json` 和 `working.secret/providers/`：本地 OpenAI 兼容供应商、模型和当前模型选择。

不会迁移旧版前端构建产物、旧 AgentScope 运行时缓存或旧的浏览器会话。这些内容由新镜像重新生成。

## 生产切换流程

1. 停止 1.1 容器，但保留其 `data/working` 和 `data/working.secret` 宿主机目录。
2. 创建全新的 `data-v2/working`、`data-v2/working.secret` 和 `data-v2/backups` 目录。
3. 先执行 `--dry-run` 检查，再去掉该参数执行真实迁移。
4. 启动 2.0.1 容器并使用新数据目录。原 1.1 目录全程不改写，可直接回退。
5. 用管理员账号逐项检查用户列表、每个用户的智能体、模型、技能、聊天记录、知识库文件和下载链接。

迁移命令示例：

```bash
python3 scripts/migrate_v1_data.py \
  --source-working /srv/huizhi/data/working \
  --source-secret /srv/huizhi/data/working.secret \
  --target-working /srv/huizhi/data-v2/working \
  --target-secret /srv/huizhi/data-v2/working.secret \
  --dry-run \
  --report /srv/huizhi/data-v2/migration-preview.json
```

该工具拒绝覆盖已存在的目标文件，因此重复运行不会破坏已经验收的数据。正式迁移前应先在服务器上的备份副本做一次演练。

## 功能取舍

保留：多用户隔离、管理员用户/模型/技能/工具授权、仅本地 OpenAI 兼容供应商、文件下载、知识库、多模态文档解析、中文品牌和离线导航。

不迁移：第三方嵌入/后台任务接口试验代码、旧前端滚动补丁、旧长对话搜索补丁、旧审批状态机和旧云模型供应商入口。2.0.1 自带 Scroll Context、会话列表虚拟化、插件系统和多智能体能力；其中 Scroll Context 解决模型上下文，不替代聊天正文渲染的性能优化，后续会单独压测。
