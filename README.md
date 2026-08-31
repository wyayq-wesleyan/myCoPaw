# 汇智小助手

这是基于官方 `v2.1.0` 代码改造的内网版智能体应用，面向没有互联网访问条件的 Ubuntu 服务器和 Docker 环境。项目保留智能体、技能、工具、文件、定时任务和会话能力，并增加多用户隔离、管理员统一管理和离线部署适配。

当前代码基线：`v2.1.0`

当前开发目录：`/Users/wesley/Desktop/projects/mycopaw-v2.0.1`

当前本机验证容器：`huizhi-v210-check`

## 1. 改造目标

- 所有用户通过浏览器使用汇智小助手，不依赖桌面模式和外部频道。
- 服务部署在内网，模型通过内网 OpenAI 兼容接口访问，例如 vLLM 或本地模型服务。
- 管理员集中配置模型、运行规则、菜单权限、技能和工具。
- 普通用户只看到自己被授权的内容，用户之间的工作区、会话和资源互相隔离。
- 代码与运行镜像分离，功能修改时只更新代码并重启容器，不反复构建大镜像。
- ARM64 用于本机测试，AMD64 用于生产服务器；底座镜像按目标架构分别准备。

## 2. 当前已完成的功能

### 2.1 本地化和品牌

- 页面品牌统一为“汇智小助手”。
- 移除页面中的官方品牌文字、在线文档、更新日志、GitHub、常见问题和语言切换入口。
- 默认使用简体中文。
- 内网版默认使用浅色主题，页面不依赖互联网功能。
- 顶部显示当前登录用户名，点击用户名可进入账户管理或退出登录。
- 左下角不再放置退出登录入口。

### 2.2 多用户和工作区隔离

- 系统支持管理员和普通用户两种角色。
- 每个用户拥有独立的用户目录、工作区、智能体、会话和配置状态。
- 普通用户只能访问自己拥有的智能体和会话。
- 创建普通用户时自动创建默认智能体。
- 新建智能体时不再手动填写工作方式、智能体 ID、工作区路径和初始技能，相关内容由系统自动生成。
- 管理员可以查看用户列表、添加用户、启用或禁用用户、删除用户和修改用户密码。
- 普通用户的账户管理只保留修改密码能力。

### 2.3 管理员权限和普通用户菜单

管理员统一管理以下内容：

- 模型和模型供应商
- 普通用户的模型授权
- 技能和工具权限
- 普通用户的菜单权限
- 全局运行配置
- 特定普通用户的运行配置覆盖项

普通用户默认只开放日常使用菜单：

- 聊天
- 知识空间
- 数据分析
- 收件箱
- 会话
- 定时任务
- 文件
- 技能
- 工具
- 智能体管理
- 词元消耗

模型、供应商、环境变量、心跳、技能池、MCP、ACP、应用中心、插件管理、频道、会话调试等管理型菜单默认收归管理员。管理员可以在用户管理中按用户开放其他菜单。

### 2.4 模型权限

模型权限采用“管理员配置、普通用户授权”的方式：

1. 管理员在模型设置中创建内网模型供应商。
2. 供应商使用标准 OpenAI 兼容协议配置地址、API Key 和模型。
3. 管理员在用户管理中选择允许某个普通用户使用的模型。
4. 普通用户在聊天页面只能从授权列表中选择模型。

当前模型选择器的规则：

- 管理员可以看到已配置的模型。
- 普通用户只能看到管理员授权的模型。
- 未授权模型不会通过聊天界面或切换接口使用。
- 普通用户没有授权模型时显示“暂无可用模型，请联系管理员授权模型”。
- 聊天顶部显示“供应商 - 模型名称”。
- 已移除旧版 PRO/FREE 分组、在线供应商入口、OAuth 入口和 API Key 引导。
- 没有配置供应商时列表为空，这是正常状态，不会自动出现官方初始模型。

后端接口也会过滤供应商和模型，并在切换模型时再次校验权限。即使浏览器缓存了旧模型，也无法通过接口绕过授权。

### 2.5 技能、工具和资源分享

- 技能和工具权限按用户账号管理，同一账号下的所有智能体共享该账号拥有的技能和工具。
- 管理员可以为用户分配或收回内置技能和工具。
- 内置技能、工具和用户后续创建的资源分开处理。
- 用户新建的技能和工具默认只有创建者和管理员可见。
- 用户可以分享自己创建的技能或工具给其他普通用户。
- 接收方通过收件箱审批，同意后资源安装到自己的资源列表。
- 资源带有创建者、来源和唯一标识，接收方不能再次转分享别人的资源。
- 普通用户也保留技能、工具和收件箱菜单，便于查看资源和处理分享请求。

### 2.6 会话功能

- 支持新建、切换和删除会话。
- 会话可以修改展示名称，手动修改后的名称不会再被自动标题生成覆盖。
- 会话支持分享，接收方审批通过后，会话内容复制到接收方的会话列表中。
- 会话消息显示对应时间。
- 聊天模型选择器支持搜索和模型能力标识。
- 当前版本使用官方 `v2.1.0` 的 Scroll Context 和会话导航能力作为长对话基础。

此前针对历史分页、滚动卡死、push-messages 频繁请求做过多轮排查。当前版本已移除部分旧版前端逻辑并以 `v2.1.0` 官方会话实现为基线，长时间运行的定时任务仍需要在实际数据量下持续验证。

### 2.6A 数据分析模块

独立的数据分析能力，支持用户上传结构化数据并通过自然语言查询：

- 支持 CSV、Excel（XLSX/XLS）文件上传，自动识别列结构。
- 数据表管理：创建、删除、批量删除、查看表结构和数据预览。
- 列定义：支持自定义列名、列描述、数据类型（文本/数字/日期）。
- 智能体工具集成：`list_data_tables`、`describe_data_table`、`query_data_table` 三个工具，支持自然语言查询数据。
- 文件名保留中文字符，自动跳过表头行智能识别。
- 每个数据表记录解析方式（`backend` 字段），便于排查问题。

### 2.6B 知识空间改进

- 知识空间列表支持批量删除。
- 文档上传后自动显示处理状态（已入库/处理中/失败）。
- 支持文档预览（PDF、DOCX 等格式）。
- 知识空间菜单图标改为书本图标（`BookOpen`），与数据分析图标区分。
- 文档解析优先使用 MinerU（PDF/DOCX 布局解析），MinerU 不可用时降级到 python-docx / pdfplumber。
- 知识服务 Worker 容器已安装 MinerU + torch，支持高质量文档解析。

### 2.6C 聊天界面优化

- 移除聊天输入框中的「默认」循环模式选择器（仅管理员可见）。
- 移除聊天输入框中的「Agent 默认」工作区选择器（仅管理员可见）。
- 移除侧边栏底部的设置齿轮图标（仅管理员可见）。
- 移除聊天右上角的「打开工作区」和「正常/宽屏切换」按钮。
- 新对话默认名称改为中文「新对话」。
- Token 消耗页面图表图例改为中文（提示词元/补全词元/总词元）。

### 2.6D 智能体管理改进

- 创建/编辑智能体时，移除「选择智能体的工作方式」（原生智能体/第三方智能体）选项，统一使用原生智能体。
- 普通用户的模型选择下拉框为只读状态，仅管理员可修改。
- 模型选择下拉框仅显示 LLM 类型模型（`model_type === "chat"`），过滤掉 Embedding、Reranker 等非对话模型。
- 创建新智能体时，自动预填全局默认模型，并在下拉选项中标注「(默认)」。
- 用户管理页面简化操作列：移除独立的「运行配置」按钮，将启用/禁用和删除用户集成到编辑弹窗中。

### 2.6E 全面中文化

- 所有英文 UI 文本翻译为中文，包括：频道管理、会话管理、收件箱、技能管理、环境变量、调试面板、Git 面板、模型管理、MCP 配置、插件管理、词元消耗等页面。
- 工具名称和描述翻译为中文（20 个内置工具）。
- 智能体循环卡片标题翻译为中文。
- 本地/远程模型提供者卡片标签翻译为中文。

### 2.7 工具审批和安全审查

根据内网使用场景，当前聊天窗口：

- 移除工具调用模式选择。
- 移除麦克风入口。
- 工具审批模式固定为 `OFF`。
- 聊天中不会弹出工具审批等待框。

这意味着系统默认不会拦截工具调用。生产环境使用前应通过账号权限、容器权限、文件挂载范围和网络策略限制风险。

### 2.8 文件和下载

- 工作区支持文件查看、上传和下载。
- 智能体生成文件后，文件应通过页面中的下载链接获取。
- 数据目录、工作区和数据库需要挂载到宿主机，否则容器重启会丢失数据。
- 当前文件工作区主要是文件管理能力，不等同于已经完成的企业业务知识库。

## 3. 离线模型和依赖环境

### 3.1 模型服务

内网模型建议使用 OpenAI 兼容接口配置，例如：

```text
供应商名称：内网模型服务
接口地址：http://模型服务器地址:端口/v1
API Key：按模型服务要求填写
模型：从服务端提供的模型列表中选择或手动添加
协议：OpenAI Chat Completions 兼容协议
```

Docker 容器访问宿主机上的模型服务时：

- Linux 生产环境优先使用 `network_mode: host`。
- 使用桥接网络时，模型地址不能填写容器内的 `127.0.0.1`。
- 宿主机模型服务必须监听可被容器访问的地址，而不能只监听宿主机回环地址。
- 模型服务的 API Key 只由管理员配置，普通用户不接触供应商凭据。

### 3.2 基础镜像

底座镜像用于承载通用 Python 和系统依赖，应用代码单独挂载。当前底座设计覆盖：

- 数据处理：`numpy`、`pandas`、`polars`、`pyarrow`、`dask`、`duckdb`、`fastparquet`
- 文件和文档：`openpyxl`、`pypdf`、`PyMuPDF`、`pdfplumber`、`python-docx`、`pillow`、`lxml`
- Web 和接口：`fastapi`、`flask`、`httpx`、`aiohttp`、`jinja2`
- 数据库：Oracle、MySQL、PostgreSQL、SQL Server、MongoDB、ClickHouse、Cassandra、Neo4j、Redis 等常用客户端
- 运维和大数据：`ansible-core`、`paramiko`、`netmiko`、`fabric`、`kubernetes`、`hdfs`、`pyhive`
- 浏览器和办公：Playwright、LibreOffice、Tesseract、Pandoc、FFmpeg
- 生产客户端：Oracle Instant Client、Hadoop 3、Hive 3 由生产环境验证过的资产提供；Hadoop/Hive 配置文件通过 Compose 挂载。

Oracle 客户端和 Hadoop/Hive 客户端属于架构相关资产，ARM64 和 AMD64 不应混用。离线构建前必须准备目标架构的压缩包或安装包。

## 4. Docker 部署

### 4.1 代码挂载模式

代码挂载模式是当前推荐的开发和测试方式：

- 镜像只包含 Python、系统依赖、浏览器和运行时环境。
- 主机代码挂载到容器 `/workspace/app`。
- 前端静态文件使用 `/workspace/app/console/dist`。
- 修改代码后重新构建前端（如果修改了前端），然后重启容器即可。
- 工作数据、密钥和备份目录必须单独挂载。

当前 ARM64 Compose 示例位于 `deploy/docker-compose.code-mount.arm64.yml`。该配置使用 `network_mode: host`，用于访问内网其他主机、宿主机模型服务以及需要通过主机名连接的生产系统。

### 4.2 持久化目录

至少需要持久化以下三个目录：

| 容器目录 | 用途 |
| --- | --- |
| `/app/working` | 用户、工作区、会话、技能、定时任务和业务数据 |
| `/app/working.secret` | 模型供应商、API Key、认证和其他敏感配置 |
| `/app/working.backups` | 备份数据 |

禁止只挂载代码而不挂载以上数据目录。容器重建或删除后，未挂载的数据无法保证保留。

### 4.3 默认认证配置

代码挂载示例默认开启认证：

```text
QWENPAW_AUTH_ENABLED=true
QWENPAW_AUTH_USERNAME=admin
QWENPAW_AUTH_PASSWORD=Admin@123456
QWENPAW_FORCE_AUTH_ON_LOCALHOST=true
QWENPAW_ENABLED_CHANNELS=console
```

生产环境应在 `.env` 或部署密钥中明确配置认证参数，并确保 `.env` 不进入代码包或 Git 仓库。

## 5. 目录和配置说明

```text
src/qwenpaw/                  后端代码
console/src/                  前端源代码
console/dist/                 前端构建产物
deploy/                       Dockerfile、Compose 和启动脚本
deploy/offline-assets/        离线构建资产说明
plugins/bundle/               可离线集成的插件资源
/app/working                  持久化业务数据
/app/working.secret           持久化密钥和模型配置
/app/working.backups          持久化备份
```

用户级状态主要保存在 `/app/working/users/<用户名>/state.json`，其中包括模型授权、菜单权限、账号技能、账号工具和运行配置覆盖标记等信息。

## 6. 已撤回或暂不迁移的功能

### 6.1 第三方 API、iframe 和后台任务 API

之前尝试过智能体对外 API、iframe 嵌入、后台任务、状态轮询、回调和临时会话管理。该方案曾出现认证、会话污染、新建聊天跳转异常、临时会话过多和结果追踪不理想等问题。

当前版本已撤回这部分后加逻辑，不应再按照旧文档配置对外 API 或 iframe。相关问题和踩坑记录应保留在独立的开发记录中，后续重新设计时再实现。

### 6.2 传统企业业务知识库

当前版本已通过独立 `knowledge-service/` 完成企业文档知识库闭环：

- 支持 PDF、DOCX、DOC、XLSX、XLS、CSV、TXT、MD、JSON 上传和异步入库。
- 支持表格、正文、公式、图片题注和视觉模型生成的图片描述检索。
- 聊天前自动按当前账号检索可访问知识空间，并保留 `query_knowledge` / `get_document_detail` 工具进行深度检索。
- PostgreSQL 与 LightRAG 均按知识空间标识过滤；未带空间标识的历史图谱记录不会进入新查询，避免跨账号泄露。

图片视觉识别需要配置支持图片输入的对话模型；文本模型不可用时仍保留图片题注和“文档图片”占位证据。

## 6A. 知识服务改造记录

### 6A.1 背景

为实现企业文档知识库能力，独立部署了知识服务（`knowledge-service/`），与汇智小助手主容器分离。知识服务负责文档解析、向量化存储和检索，通过 REST API 提供服务。

### 6A.2 初始架构（存在问题）

**初始设计：**
```
用户提问 → 智能体调用 query_knowledge 工具
    ↓
工具调用知识服务 /v1/query 接口
    ↓
知识服务执行 RAG 检索 + 模型生成 answer
    ↓
工具返回完整内容（answer + 12个证据片段）
    ↓
智能体直接使用返回内容回答用户
```

**存在的问题：**

1. **上下文污染**：工具一次性返回大量内容（answer + 证据片段），可能撑爆上下文窗口
2. **知识服务answer不可靠**：知识服务模型没有对话上下文，生成的答案可能偏离用户意图
3. **缺乏agentic特性**：智能体被动接收所有信息，无法自主决定检索策略

### 6A.3 问题分析

用户在测试中发现：当询问"天津大学硕士毕业论文格式要求"时，智能体调用 query_knowledge 工具后，虽然工具返回了相关内容，但智能体的回答却说内容是关于"多智能体协作协议"的，与问题不相关。

**根本原因：**
- 系统提示词没有明确告诉大模型如何处理工具返回的内容格式
- 大模型混淆了"知识服务生成的answer"和"检索证据"
- 大模型可能将对话上下文中的其他内容错误关联到知识库返回的内容上

### 6A.4 已完成的修复

1. **知识服务模型配置简化**
   - 移除复杂的 hub 模式，改为直接指定模型名称
   - `.env` 配置：`PROVIDER_BASE_URL`、`PROVIDER_API_KEY`、`CHAT_MODEL`、`EMBEDDING_MODEL`、`RERANKER_MODEL`
   - 模型服务从 `/v1/models` 自动发现可用模型作为备选

2. **流式回答支持**
   - 知识服务：新增 `answer_stream()` 方法和 `/v1/query/stream` SSE 接口
   - 前端：新增 `queryStream()` 方法，支持流式显示答案
   - 知识空间页面现在支持流式显示检索结果

3. **引用证据简化**
   - 知识空间页面的证据列表只显示文件名和页码，不再显示大段原文
   - 减少上下文占用

4. **系统提示词优化**
   - 在 `prompt_contributors.py` 中明确工具返回格式说明
   - 告诉大模型：`answer` 是知识服务模型生成的，`检索证据` 是实际源文档
   - 强调应使用检索证据回答问题，不要混淆知识库内容与对话上下文

### 6A.5 待解决的架构问题（已解决）

**已完成的重构：**

1. **知识服务接口重构**
   - 新增 `/v1/search` 纯检索接口，只返回检索结果，不生成 answer
   - 新增 `/v1/documents/chunks` 接口，支持按需获取文档详情
   - 保留原有 `/v1/query` 接口（向后兼容）

2. **query_knowledge 工具重构**
   - 使用新的 `/v1/search` 接口
   - 返回文档列表和摘要，而非完整内容
   - 返回格式：文档ID、文件名、相关片段数量、涉及页码

3. **新增 get_document_detail 工具**
   - 支持按需获取特定文档的详细内容
   - 支持按页码范围过滤
   - 支持限制返回的片段数量

4. **系统提示词优化**
   - 明确告诉大模型工具的使用流程
   - 强调必须先调用 query_knowledge，再调用 get_document_detail
   - 避免大模型混淆知识库内容与对话上下文

**新的工作流程：**

```
用户提问 → 智能体推理：需要检索知识库
    ↓
调用 query_knowledge(query="...")
    ↓
知识服务返回：文档列表和摘要（不包含完整内容）
    ↓
智能体分析：哪些文档相关？需要查看哪些？
    ↓
调用 get_document_detail(document_id="...")
    ↓
知识服务返回：指定文档的详细片段
    ↓
智能体综合所有信息 + 对话上下文 → 生成最终回答
```

**优势：**

1. **上下文可控**：智能体可以分批获取信息，避免一次性加载过多内容
2. **决策自主**：智能体根据对话上下文和检索结果自主决定下一步行动
3. **职责清晰**：知识服务负责存储和检索，智能体负责推理和生成
4. **可扩展性**：可以添加更多检索工具（如按文档类型、按时间范围、按关系检索等）

### 6A.6 后续开发计划（部分已完成）

**已完成：**

1. **重构 query_knowledge 工具** ✅
   - 使用新的 `/v1/search` 接口
   - 返回文档列表和摘要，而非完整内容
   - 智能体需要主动调用其他工具获取详细内容

2. **新增检索工具** ✅
   - `get_document_detail(document_id, page_start?, page_end?, max_chunks?)`：获取指定文档的详细内容
   - 支持按页码范围过滤
   - 支持限制返回的片段数量

**待完成：**

3. **优化检索策略**
   - 支持多轮检索：智能体可以根据初步结果调整查询
   - 支持过滤条件：按文档类型、时间范围、相关度阈值等过滤
   - 支持检索结果缓存：避免重复检索相同内容

4. **前端适配**
   - 知识空间页面的检索功能需要与新的检索接口适配
   - 支持显示检索过程和中间结果

5. **新增更多检索工具**
   - `search_by_relation(entity_name)`：基于知识图谱的关系检索
   - `get_similar_documents(doc_id)`：查找相似文档
   - `list_documents(space_id)`：列出知识空间中的所有文档

## 7. 推荐验收流程

1. 使用管理员登录，确认模型设置中只显示当前配置的内网供应商。
2. 添加一个普通用户，为其授权一个模型、部分技能、工具和菜单。
3. 使用普通用户登录，确认模型选择器只显示授权模型。
4. 移除该用户的模型授权，确认聊天选择器显示“暂无可用模型”。
5. 创建普通用户自己的技能或工具，确认其他普通用户不可见。
6. 分享技能或工具，使用接收方在收件箱审批，确认审批后资源出现在接收方所有智能体中。
7. 创建并切换多个会话，修改会话名称，确认名称不会被自动覆盖。
8. 上传和生成文件，确认页面显示下载链接，并在宿主机挂载目录中可以找到文件。
9. 重启容器，确认用户、模型、会话、技能、工具和文件仍然存在。
10. 在长会话和长定时任务场景下验证滚动、加载和浏览器资源占用。

## 8. 开发和验证

前端修改后：

```bash
cd console
npm run build
npm exec vitest run src/pages/Chat/ModelSelector/ModelSelector.test.tsx
```

后端修改后：

```bash
python -m compileall -q src/qwenpaw
```

代码挂载容器重启后检查：

```text
http://127.0.0.1:18088/
```

版本接口：

```text
http://127.0.0.1:18088/api/version
```

## 9. 近期开发记录（接手以来）

### 9.1 知识服务架构重构

#### 问题背景
初始设计中，知识服务返回完整 answer + 证据片段，导致：
- 上下文污染：一次性返回大量内容可能撑爆上下文窗口
- answer 不可靠：知识服务模型没有对话上下文，生成的答案可能偏离用户意图
- 缺乏 agentic 特性：智能体被动接收所有信息，无法自主决定检索策略

#### 已完成的重构

**1. 纯检索接口**
- 新增 `/v1/search` 接口，只返回检索结果，不生成 answer
- 新增 `/v1/documents/chunks` 接口，支持按需获取文档详情
- 保留原有 `/v1/query` 接口（向后兼容）

**2. 智能体工具重构**
- `query_knowledge` 工具：使用新的 `/v1/search` 接口，返回文档列表和摘要
- 新增 `get_document_detail` 工具：支持按需获取特定文档的详细内容
- 支持按页码范围过滤和限制返回片段数量

**3. 新的工作流程**
```
用户提问 → 智能体调用 query_knowledge
    ↓
知识服务返回：文档列表和摘要
    ↓
智能体分析：哪些文档相关？
    ↓
调用 get_document_detail
    ↓
知识服务返回：指定文档的详细片段
    ↓
智能体综合生成最终回答
```

**4. 流式回答支持**
- 知识服务：`answer_stream()` 方法和 `/v1/query/stream` SSE 接口
- 前端：`queryStream()` 方法，支持流式显示答案

### 9.2 知识空间前端重构

**页面布局**
- 左侧面板：知识空间选择 + 搜索框
- 右侧面板：文档列表，支持分页（每页10个）

**文档预览功能**
- 支持 PDF 预览：使用 `pdfjs-dist` 库，代码分割加载
- 支持 DOCX 预览：使用 `mammoth` 库，转换为 HTML 显示
- 支持 TXT 预览：直接显示纯文本内容
- 预览方式：使用 Ant Design Drawer 组件（720px 宽度）

**API 新增**
- `/v1/documents/{id}/file`：获取原始文件（知识服务）
- `/api/knowledge/file-proxy`：文件代理（主应用）

### 9.3 模型配置修复

**问题**
- `llm_routing.local.provider_id` 错误配置为 `"openai"`，实际应为 `"llm"`
- API key 加密解密失败（master key 变更导致密文无法解密）

**修复**
- 将 provider_id 改为 `"llm"`
- 将加密的 API key 替换为明文 `"thinker"`
- 路径：`/app/working.secret/providers/custom/llm.json`

### 9.4 系统提示词优化

**创建的文件**
- `AGENTS.md`：Agent 行为规范，明确工具使用规则
- `SOUL.md`：助手性格特征定义
- `PROFILE.md`：基本信息

**关键指令**
- 必须使用工具返回的结果回答，禁止编造
- 明确引用知识空间名称和文档来源
- 如果知识库中没有相关信息，如实告知

### 9.5 收件箱推送设置

已禁用以下自动推送功能：
- `auto_memory_inbox_push_enabled`: false
- `auto_dream_inbox_push_enabled`: false
- `daily_paper_inbox_push_enabled`: false

配置路径：`/app/working/config.json`

### 9.6 知识服务第二轮改造：MinerU / LightRAG / PG17 与生产化加固

背景：第一轮知识服务搭建完成了基础的 `LocalParser` 解析 + BM25 检索 + LiteLLM 路由。
在实际导入学术论文 PDF 和技术规范文档时，暴露了三个核心问题：

1. 解析质量差：`LocalParser` 只提取纯文本，公式变成乱码、表格丢失、图片题注丢失、
   中文 PDF 阅读顺序错乱。90 页 MBA 论文只提取出 39 个 chunk，公式全部变成 `$$...$$` 乱码
   （如 `$ - \\Lambda \\zeta^{\\star}$`），表格结构完全丢失。
2. 检索失效：PG 的 `simple` FTS 配置对中文无分词能力，整句变一个 token。
   `ILIKE '%词%'` 只做子串匹配，"论文格式要求"四个字被当作一个不可分割的 token 存储，
   搜索"论文格式"永远匹配不到。语义检索无相关性下限，问"今晚吃什么"返回一堆无关答案。
3. 缺乏知识图谱：实体关系完全依赖启发式抽取（正则 `发明|发现|提出|创建`），
   只能捕获 "X 发现了 Y" 这种固定句式。跨文档关联、隐含实体推断、实体类型识别均为空白。

改动清单（按重要性排序）：

**A. 解析管线：LocalParser → MinerU 强制管线**

背景：MinerU（magic-pdf）是 MinerU 团队开源的文档解析工具，基于 UniMERNet 模型，
能精确提取 PDF 中的公式（LaTeX）、表格（Markdown/HTML）、图片题注、以及保留正确阅读顺序。
之前只对 PDF 启用 MinerU，DOC/DOCX 仍走 LocalParser，导致 Word 文档质量极差。

改动原因：
- DOC/DOCX 用 python-docx 读取只能拿到 `<w:p>` 段落文本，表格变成拼接字符串，
  图片直接丢失，多栏布局的阅读顺序无法保证。实测一份论文格式规范 DOCX 用 LocalParser
  只提取出 6 个 chunk（含大量重复/乱序文本）。
- LibreOffice headless 可将 DOC/DOCX 无损转为 PDF（保留表格/图片/布局），
  转换后交给 MinerU 处理，公式/表格/图片题注全部正确提取。
- `parse_document()` 新增 `parsing_method` 参数，`None`=按文件类型自动路由，
  `"pdf_mineru"`/`"doc_libreoffice_mineru"` 强制指定。
- LibreOffice 常驻进程（subprocess STARTUPINFO）避免每次转换都冷启动。

改动后效果：
- DOCX：214 个 chunk（原来 6 个），含 5 个表格、5 个图片、真实页码
- PDF：393 个 chunk（原来 39 个），含 12 个公式块、26 个表格、图片题注
- 转换耗时：LibreOffice DOCX→PDF ~5-25 秒（取决于文档复杂度）

涉及文件：
- `knowledge-service/app/parsers.py`：`LocalParser` 重命名为 `SimpleReader`（仅处理
  TXT/MD/CSV/XLSX）；新增 `_convert_to_pdf()` LibreOffice 转换函数；
  `parse_document()` 新增路由逻辑强制 DOC/DOCX 走 MinerU 管线
- `knowledge-service/Dockerfile`：新增 `apt-get install libreoffice-core libreoffice-writer
  libreoffice-calc` 安装 LibreOffice；Python 依赖新增 `mineru[core]==3.4.5`
  `torch==2.8.0+cpu` `torchvision==0.23.0+cpu`
- `knowledge-service/.env`：新增 `INSTALL_KNOWLEDGE_EXTRAS=1`
  `SUPPORTED_DOC_EXTENSIONS` 加入 `.doc`
- `knowledge-service/app/config.py`：`DEFAULT_SUPPORTED_DOC_EXTENSIONS` 加入 `.doc`

**B. 数据存储：SQLite → PostgreSQL 17**

背景：第一轮用 SQLite 存储 chunks/embeddings/entities/relations。SQLite 的问题：
- 单文件锁（WAL 模式下仍限制并发写入），多 worker 同时入库会排队
- JSONB 向量检索（`json_each`）在 SQLite 下更慢（无并行扫描）
- 无 `pg_trgm` 等扩展支持，后续做模糊匹配受限
- 无原生复制/备份工具，生产环境风险高

改动原因：
- PostgreSQL 17 原生支持 JSONB 数组操作、GIN 索引、pg_trgm 模糊匹配
- `psycopg_pool` 连接池复用连接（min 2 / max 20），避免每次操作新建连接
  （30-50 人并发下单次检索开 4 个连接，无池会打爆 PG 默认 100 上限）
- `SKIP LOCKED` 队列支持多 worker 扩展（当前单 worker 够用，未来可横向加）
- 保留 `migrate_sqlite_to_postgres.py` 迁移工具

涉及文件：
- `knowledge-service/app/store.py`：`Store` 类重写，所有 SQL 改为 PostgreSQL 语法
  （`RETURNING *` / `jsonb_array_elements_text` / `COPY ... TO STDOUT` /
  `psycopg_pool.ConnectionPool`）
- `knowledge-service/docker-compose.yml`：新增 `huizhi-knowledge-postgres` 服务
  （`postgres:17-alpine`，`POSTGRES_PASSWORD=knowledge2026`）
- `knowledge-service/pyproject.toml`：依赖新增 `psycopg[binary,pool]>=3.2`

**C. 检索质量：三个关键修复**

背景：第一轮的检索对中文几乎不可用，三个独立问题叠加导致检索结果极差。

修复 1：PG 全文检索对中文完全失效
- 原因：PostgreSQL 的 `simple` 配置无中文分词能力，"论文格式要求"被当作一个不可分割的
  token 存储。搜索"论文格式"永远不会匹配到这个 token（除非完全一致）。
- 改动：用 Python `_cjk_bigrams()` 做二元组切分，"论文格式要求"→`[论文,文格,格式,式要,要求]`，
  存入 `text_tokens` 列。搜索时计算覆盖率打分，命中率 40% 以上纳入结果。
- 效果：搜索"论文格式"现在能正确匹配到含"论文格式要求"的 chunk。

修复 2：语义检索无相关性下限
- 原因：Qwen3-Embedding 的余弦分普遍偏高（相关查询 ≥0.45，无关查询 ≤0.36），
  无下限时问"今晚吃什么"会返回一堆关于"吃"的无关知识片段。
- 改动：`SEMANTIC_MIN_SCORE = 0.40`（实测标定），低于此分的语义结果直接丢弃。
  本地字面/语义两路全空时视为整库无关，图谱结果一并丢弃。
- 效果：实测"吃什么饭"、"今天天气怎么样"均返回 0 条结果，"论文格式要求"返回 20+ 条。

修复 3：LightRAG 图谱结果无条件拼入导致噪声
- 原因：LightRAG 的 `/query/data` 不返回相关性分数，无法判断结果质量。
  本地检索无结果时（说明查询与知识库无关），图谱结果仍被拼入，导致噪声。
- 改动：本地字面+语义两路全空时，图谱结果一并丢弃。图谱结果保留约 1/3 配额
  （`max(2, top_k // 3)`），避免被低质量字面匹配挤占。
- 效果：图谱结果只在本地有匹配时作为补充，不会在无关查询时引入噪声。

涉及文件：
- `knowledge-service/app/store.py`：`search()` 重写为 CJK 二元组覆盖率打分；
  `search_by_embedding()` 改用 numpy 向量化 + `min_score` 参数；
  新增 `_cjk_bigrams()` 工具函数
- `knowledge-service/app/main.py`：`_hybrid_search()` 新增 LightRAG 配额逻辑；
  `SEMANTIC_MIN_SCORE = 0.40`

**D. LightRAG 图谱检索集成**

背景：LightRAG（Hong Kong U 开源）基于 LLM 构建实体-关系知识图谱，
能回答"哪些人参与了 XX 项目"这类跨文档关联问题。之前因认证、模型绑定、
连接地址等问题完全不可用，检索时 `lightrag.reachable` 一直是 `false`。

改动原因：
- LightRAG 使用 `X-API-Key` 头认证（不是 Bearer token），之前发 Bearer 头
  导致 401。
- LightRAG 的 `EMBEDDING_MODEL` 默认绑定 `text-embedding-3-small`（OpenAI），
  需设为空串让它用 `/v1/embeddings` 接口 + 自定义模型名。
- `llm_binding` 和 `embedding_binding` 的 `api_key` 必须显式设置为 `thinker`
  （master key 加密方案变更后，环境变量传入的是明文）。
- 容器内 `lightrag-hku` 安装了但 `lightrag-server` 命令找不到（安装路径问题），
  改用 `python -m lightrag.server` 启动。
- `reindex_space()` 实现：遍历指定知识空间的所有 chunks，逐条调用 LightRAG
  `insert()` 构建图谱。409 冲突视为成功（重复文档）。

改动后效果：
- `GET /health` 返回 `lightrag.reachable: true`、`entities: 558+`、`docs: 3`
- 语义改写后的查询（如"论文格式要求"→"学术论文的格式规范"）能同时命中
  本地 BM25 + LightRAG 图谱，跨文档实体关联正常工作
- 文档处理完成后自动触发 `insert`，无需手动 reindex

涉及文件：
- `knowledge-service/app/lightrag_bridge.py`：`LightRAGBridge` 类重写，
  `X-API-Key` 头认证 + `/query/data` fast path + `reindex_space()` 实现
- `knowledge-service/docker-compose.yml`：新增 `huizhi-lightrag` 服务
  （`lightrag-hku` + `lightrag-server`）
- `knowledge-service/.env`：新增 `LIGHTRAG_URL=http://lightrag:9621`
  `LIGHTRAG_API_KEY=thinker` `OPENAI_API_KEY=thinker`

**E. 性能优化：为 30-50 人正式环境准备**

背景：第一轮单次 PDF 处理 3 分 37 秒，实体抽取串行 6 批耗时 4.4 分钟（占 32%）。
Embedding 单次全量调用 214 块（易超时）。向量检索用 Python 循环计算余弦（慢）。
无连接池，每次操作新建 PG 连接。Async 端点里直接同步调用 store（阻塞事件循环）。

改动 1：实体抽取分批并行
- 原因：之前 prompt 硬截断 5 万字符，大文档超过一半内容不参与实体抽取。
  改为分批（12k 字/批）后，354 块分 6 批串行调用 LLM，每批 30-60 秒，
  总计 4.4 分钟。
- 改动：`ThreadPoolExecutor(max_workers=3)` 并行抽取。MLX 模型服务单卡串行处理，
  但 Python 线程可重叠 I/O 等待和后处理。
- 效果：docx 214 块处理从 3 分 37 秒降到 1 分 30 秒（2.4 倍提速）。

改动 2：Embedding 分批调用
- 原因：单次调用 214 块 embedding（~40KB 文本）容易超时或 OOM。
- 改动：32 块/批，每批独立调用 `/v1/embeddings`。
- 效果：354 块 embedding 仅需 47 秒（原 214 块截断方案 30 秒，但覆盖不全）。

改动 3：向量检索 numpy 向量化
- 原因：Python 循环 `sum(a*b for a,b in zip(...))` 计算余弦，400 块需 400 次循环。
- 改动：`numpy` 向量化 `np.dot / (norm_a * norm_b)`，一次运算处理全量。
- 效果：向量检索耗时从 ~50ms 降到 ~5ms（400 块规模）。

改动 4：数据库连接池
- 原因：每次 DB 操作新建连接（一次混合检索开 4 个连接），30-50 人并发下
  连接风暴。PostgreSQL 默认 `max_connections=100` 会被打穿。
- 改动：`psycopg_pool.ConnectionPool(min_size=2, max_size=20)`，透明接入
  所有 store 方法。压测后 PG 中保持 11 个复用连接。
- 效果：连接复用率从 0% 提升到 ~100%，无连接风暴风险。

改动 5：事件循环阻塞消除
- 原因：FastAPI `async def` 端点里 16 处直接同步调用 `store.*`（DB 操作），
  阻塞整个事件循环。所有用户请求被串行化，并发时延迟线性叠加。
  这是最隐蔽的扩展性杀手——开发时单用户无感知，30-50 人并发时全部卡死。
- 改动：所有同步调用改为 `await asyncio.to_thread(store.xxx, ...)`，
  FastAPI 默认 40 线程池承接并发。
- 效果：8 并发检索请求 100% 成功（22.7 秒内完成，含模型服务排队）。

改动 6：失败任务自动重试
- 原因：MinerU 模型加载偶尔失败（内存不足/OOM），任务永久失败需手动重试。
- 改动：`MAX_JOB_ATTEMPTS = 2`，失败任务自动重新入队一次，10 秒退避。
- 效果：模型加载失败后自动重试，无需人工干预。

涉及文件：
- `knowledge-service/app/providers.py`：`extract_facts()` 新增 `_extract_batch()` +
  `ThreadPoolExecutor(max_workers=3)` 并行抽取；`embed()` 改为 32 块/批调用
- `knowledge-service/app/store.py`：`search_by_embedding()` 改用 numpy 向量化；
  新增 `_cjk_bigrams()` + `ConnectionPool`
- `knowledge-service/app/worker.py`：新增 `requeue_job_for_retry()` +
  `MAX_JOB_ATTEMPTS = 2`

**F. Console 前端改动**

改动 1：知识空间菜单固定开放给所有用户
- 原因：知识空间是标准功能，不应需要管理员单独授权。
- 改动：`builtinMenu.ts` 将 `core.knowledge` 移出 `core.workspace-group` 子菜单，
  提升为一级菜单项；`Sidebar.tsx` 加入 `ALWAYS_USER_MENU_IDS`；
  `MainLayout/index.tsx` 加入 `ALWAYS_ALLOWED_ROUTE_IDS`；
  `UserManagement/index.tsx` 知识空间 checkbox 设为 disabled + 默认勾选。

改动 2：Agent 配置 maxInputLength 只读展示
- 原因：用户不应手动修改 maxInputLength（由所选模型决定），但需要看到当前值。
- 改动：`LightContextCard.tsx` 新增 disabled InputNumber 展示当前值，
  提示文字"当前值来自所选模型；请在设置 → 模型中修改"。

改动 3：`AgentRunningConfig` 类型新增 `max_input_length` 可选字段
- 原因：TypeScript 类型定义需与后端运行时配置对齐。
- 改动：`agent.ts` 新增 `max_input_length?: number`。

**G. 上下文管理改进**

改动 1：`ConversationScopeContributor` 新增
- 原因：多轮对话中，模型容易把内部生成的 `visual_context`/`visual_history` 页面
  误认为是用户刚上传的图片，产生"您刚才发的这张图"这类幻觉。
- 改动：新增 prompt contributor 明确区分"当前用户消息"和"内部上下文快照"，
  指示模型"不要把内部页面归属于用户"。优先级 82（在 MultimodalHint 之前注入）。
- 涉及文件：`src/qwenpaw/runtime/prompt_contributors.py`
- 测试：`tests/unit/runtime/test_prompt_contributors.py` 新增
  `test_conversation_scope_distinguishes_internal_visual_history`

改动 2：视觉历史页面标题强化
- 原因：`visual_history` 页面标题为 "EARLIER TURNS OF THIS CONVERSATION"，
  模型仍可能误认为是用户刚提供的内容。
- 改动：标题改为 "INTERNAL QWENPAW VISUAL HISTORY SNAPSHOTS — NOT USER-UPLOADED IMAGES"，
  尾部追加"Never attribute these snapshots to the user"。
- 涉及文件：`src/qwenpaw/agents/context/visual_compression/pipeline/history.py`

改动 3：静态上下文页面标题强化
- 原因：与改动 2 同理，`static_context` 的渲染页面也需要明确标注为内部生成。
- 改动：标题追加 "INTERNAL QWENPAW CONTEXT SNAPSHOTS — NOT USER-UPLOADED IMAGES"。
- 涉及文件：`src/qwenpaw/agents/context/visual_compression/pipeline/static_context.py`

## 10. 当前状态摘要

| 项目 | 状态 |
| --- | --- |
| 官方基线 | `v2.1.0` |
| 本地化品牌 | 已完成 |
| 多用户和工作区隔离 | 已完成 |
| 管理员用户管理 | 已完成 |
| 普通用户菜单权限 | 已完成 |
| 管理员模型授权 | 已完成 |
| 普通用户模型选择限制 | 已完成 |
| 技能和工具账号级权限 | 已完成 |
| 技能、工具分享审批 | 已完成 |
| 会话分享审批 | 已完成 |
| 代码挂载部署 | 已完成，ARM64 已验证 |
| 容器数据持久化 | 已完成，必须正确挂载 |
| 第三方 API/iframe | 已撤回 |
| 知识服务部署 | 已完成，4 容器架构（api / worker / postgres / lightrag） |
| 知识服务纯检索接口 | 已完成 `/v1/search` |
| 按需获取文档详情 | 已完成 `get_document_detail` |
| 知识空间流式查询 | 已完成前端流式显示 |
| 知识库智能体集成 | 已完成重构，支持多轮检索 |
| 账号/空间检索隔离 | 已完成，LightRAG 使用 `knowledge://<space>/<document>/...` 标识 |
| 文档预览（PDF/DOCX/TXT） | 已完成，使用 Drawer 组件 |
| 模型配置自动发现 | 已完成，从 provider 获取模型列表 |
| 系统提示词 | 已创建 AGENTS.md/SOUL.md/PROFILE.md |
| MinerU 文档解析 | 已完成，PDF/DOCX 全部走 MinerU 管线 |
| XLS/XLSX/CSV/JSON 解析 | 已完成，支持表格和结构化 JSON 展开 |
| 文档图片视觉描述 | 已完成，需配置视觉模型 |
| LightRAG 知识图谱 | 已完成，558+ 实体，业务集成 |
| PostgreSQL 17 存储 | 已完成，连接池 2-20 |
| CJK 二元组检索 | 已完成，替代失效的 PG FTS |
| 语义相关性过滤 | 已完成，阈值 0.40 |
| 实体抽取并行化 | 已完成，3 线程 2.4x 提速 |
| 长会话性能 | 以官方 Scroll Context 为基础，需继续验证 |

## 11. 维护原则

- 后续功能必须基于 `v2.1.0` 主线继续开发，不要回退到 `1.1.12`。
- 不要修改或覆盖旧版生产数据，升级前先备份 `/app/working`、`/app/working.secret` 和 `/app/working.backups`。
- 不要把 `.env`、API Key、认证文件和用户数据打进代码包。
- ARM64 和 AMD64 镜像、Oracle 客户端、浏览器运行时及 Hadoop/Hive 客户端必须分别验证。
- 代码变更优先采用挂载代码模式测试；只有依赖或底座变化时才重新构建镜像。
- 每次前端改动后都要重新构建 `console/dist` 并重启容器，否则浏览器仍可能加载旧的静态资源。

## 12. 知识服务部署

### 11.1 容器信息

- 容器名称：`huizhi-knowledge-api`
- 端口映射：`18100:18100`
- 数据目录：`/data`
- 代码目录：`/opt/huizhi-knowledge/app/`

### 11.2 模型配置

知识服务使用 OpenAI 兼容接口，模型不打进镜像：

```text
对话模型：gemma-4-26B-A4B-it-MLX-4bit
Embedding：Qwen3-Embedding-0.6B-8bit
Reranker：Qwen3-Reranker-0.6B-4bit
接口地址：http://host.docker.internal:9988/v1
API Key：thinker
```

### 11.3 常用命令

```bash
# 重启知识服务
docker restart huizhi-knowledge-api

# 查看日志
docker logs -f huizhi-knowledge-api

# 进入容器
docker exec -it huizhi-knowledge-api bash

# 热更新代码（无需重建镜像）
docker cp knowledge-service/app/main.py huizhi-knowledge-api:/opt/huizhi-knowledge/app/
docker exec huizhi-knowledge-api rm -rf /opt/huizhi-knowledge/app/__pycache__
docker restart huizhi-knowledge-api

# 重建镜像（需要时）
cd knowledge-service
docker compose build --no-cache
docker compose up -d
```

### 11.4 API 接口

```text
GET  /health                          健康检查
POST /v1/search                       纯检索（推荐）
POST /v1/query                        RAG 查询（含 answer）
POST /v1/query/stream                 流式 RAG 查询
GET  /v1/documents/{id}/chunks        获取文档分块
GET  /v1/documents/{id}/file          获取原始文件
POST /v1/documents/upload             上传文档
GET  /v1/spaces                       列出知识空间
GET  /v1/spaces/{id}/documents        列出空间文档
```

### 11.5 已知问题

- gemma-4 模型在长上下文中可能被旧内容干扰，建议开启新会话测试
- API key 加密解密失败时需手动替换为明文
- 文档预览需要知识服务返回文件内容，确保文件路径可访问
