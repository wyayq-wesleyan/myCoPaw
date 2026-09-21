# 汇智小助手

这是基于官方 `v2.1.0` 代码改造的内网版智能体应用，面向没有互联网访问条件的 Ubuntu 服务器和 Docker 环境。项目保留智能体、技能、工具、文件、定时任务和会话能力，并增加多用户隔离、管理员统一管理和离线部署适配。

当前代码基线：`v2.1.0`

当前开发目录：`/Users/wesley/Desktop/projects/huizhi-assistant-v2.2.1`

当前本机验证容器：`huizhi-v210-check`

> 本文件是本项目当前实现、部署约束和已知问题的主说明。README 中较早的
> “新闻”、官方安装方式和上游 QwenPaw 介绍仅用于保留来源信息；正式环境应以
> 本文件的“项目背景与正式环境实施记录”、部署文件和 `.env` 为准。

## 项目背景与正式环境实施记录

### A. 真实使用场景和验收目标

本项目不是单用户演示程序，而是部署在内网服务器上的多用户系统。通常同时有
8–10 名用户登录，每个人会创建会话、上传文档、运行智能体、使用知识空间和数据
分析。少数用户可能同时处理大 PDF、Excel 或 CSV，或者让智能体编写 Python 脚本
执行长时间数据处理任务。

正式环境的最低设计目标如下：

- 用户之间的账号、会话、智能体、文件、技能、工具、知识空间和数据表必须隔离。
- 普通用户只能读取自己的用户路径和被明确分享给自己的资源；不能通过菜单、接口
  或智能体工具读取其他用户文件。
- 长任务不能因为浏览器暂时断线而被误判为失败；刷新页面后应能看到已产生的内容。
- 服务重启或升级时，知识空间和数据分析的处理中任务要能恢复；失败任务按每小时
  重试，初次执行失败后最多再重试 3 次，仍失败则保留失败状态和可下载日志片段。
- 文档、图表、Word、Excel、PDF 等产出默认在聊天消息中提供下载链接，而不是只写
  到用户无法访问的容器后台目录。
- 服务器建议至少 256 GB 内存、30 个 CPU 核和 4 TB 磁盘。Docker 的资源上限必须
  根据实际机器在 `.env` 中调整，并预留操作系统、PostgreSQL、LightRAG 和模型服务
  的资源，不能把宿主机全部内存无条件分给单个容器。

### B. 系统组成

```text
浏览器
  └─ 汇智小助手主应用（FastAPI + Console 前端）
       ├─ 用户/权限/会话/文件/技能/工具/定时任务
       ├─ 智能体运行时、模型流式输出和文件下载
       └─ 知识服务 REST API
            ├─ 知识服务 API
            ├─ 知识服务 Worker（MinerU、Embedding、实体抽取）
            ├─ PostgreSQL 17（可独立 Compose 维护）
            └─ LightRAG（知识图谱，可选）
```

主应用和知识服务代码均采用 code-mount 方式更新：底座镜像保存系统依赖，代码包
替换宿主机的 `app` 目录后重启容器。数据库、密钥、用户数据和日志必须独立挂载，
不得打进代码包。

### C. 改造过程和踩过的坑

#### 1. 单用户逻辑不能直接用于多人环境

早期实现把 `default`、工作区路径和会话状态当成全局值，存在串数据风险。后续将
用户名映射为稳定的内部账号标识（中文用户名也能正常登录），并在用户目录、会话、
智能体、文件、技能、工具、知识空间和数据分析数据库层逐层校验归属。管理员也不
能借普通用户的会话标识读取他人数据；只有明确的分享审批才会产生访问关系。

#### 2. 大文件不能一次性读入内存

初始知识服务和聊天附件路径会把整份文件、所有 embedding 或所有检索证据一次性
放进内存，多个用户同时上传时容易触发 OOM 或长时间阻塞。现在采用 8 MB 上传块、
CSV 分块读取、XLSX read-only、Embedding 32 块一批、有限大小的检索结果和聊天附件
上限。知识空间单文件上限为 10 GB；聊天当前轮附件文本和图片另有更严格的上下文
上限，避免把大文件直接塞进模型上下文。

#### 3. SQLite 和同步数据库调用不适合并发入库

SQLite 单文件写锁、无中文分词和每次操作新建数据库连接在多人场景下会放大延迟。
已改用 PostgreSQL 17、连接池（默认 2–20）、CJK 二元组检索、向量化计算，并把
FastAPI 中的同步数据库操作移到线程池，避免一个用户的导入阻塞所有请求。

#### 4. 模型或解析服务长时间无输出会导致页面断线

单纯依赖 HTTP 超时会出现“页面一直等待，稍后白屏/崩溃；刷新后只剩问题”的现象。
现已加入：

- SSE 心跳，默认每 10 秒发送一次；
- LLM 首内容和稳定阶段空闲看门狗；
- 流式事件有界缓冲和磁盘短期恢复副本；
- 任务结束后保留约 10 分钟重放内容，应用进程重启后也尝试从磁盘恢复；
- 前端对损坏 JSON、未知事件和缺少 `msg_id` 的帧做容错；
- 长重连内容按批次快进；
- Chat 渲染区错误边界，单个消息或工具卡失败时不应拖垮整个页面。

#### 5. Docker 启动和日志挂载曾导致容器一秒重启

代码挂载入口脚本、旧底座镜像中的 Supervisor 模板和日志目录挂载必须匹配。入口
脚本不可执行时，必须使用 `exec /bin/sh /entrypoint.sh` 方式启动；旧镜像不支持新
日志模板时，挂载入口脚本会造成启动循环。升级前应先用目标架构镜像执行配置检查，
确认容器能稳定运行后再替换正式目录。应用日志、Supervisor 日志和子进程日志现在
支持写入宿主机目录，不能只依赖容量有限的 `docker logs`。

#### 6. 知识库检索初期会返回无关答案

PostgreSQL `simple` 全文检索对中文没有分词能力，LightRAG 又会在没有相关性分数时
拼入噪声结果。现在采用中文二元组覆盖率、语义最低分 0.40、本地检索为空时丢弃图谱
结果，并把“先搜索、再按需读取文档详情”的流程交给智能体决策。

### D. 当前资源分配建议

Compose 中的 `mem_limit` 和 `cpus` 是上限，不是常驻占用。当前默认值为：主应用
48 GB/8 核，知识服务 API 16 GB/4 核，Worker 80 GB/14 核，PostgreSQL 32 GB/4 核，
LightRAG 32 GB/4 核。总上限约 208 GB/34 核，仍需根据模型服务是否独立部署调整。

如果希望明确给这套程序预留约 200 GB 内存和 30 核 CPU，应在正式 `.env` 中设置并
压测确认，例如提高主应用的 `QWENPAW_MEMORY_LIMIT`/`QWENPAW_CPU_LIMIT`，同时不要
让多个容器上限简单相加超过宿主机可用资源。模型推理若运行在同一台服务器，还必须
单独为模型预留内存和 CPU/GPU，不能把 Docker 上限当成模型可用资源。

### E. 已完成、已缓解和未完成事项

#### 已完成并有自动化或本地验证

- 多用户目录、会话、智能体、技能、工具、知识空间和数据分析的归属校验。
- 中文用户名的稳定内部标识和登录显示兼容。
- 普通用户文件菜单及后端路径限制；普通用户不能读取其他用户文件。
- 指定接收人的技能/工具/会话分享审批。
- PostgreSQL 17、连接池、流式大文件上传和每用户数据分析数据库。
- MinerU/LibreOffice 文档解析、中文检索、LightRAG 隔离和文档级联清理。
- 失败任务每小时重试，初次失败后最多 3 次；错误日志片段可在页面下载。
- 聊天文件自动解析，Word/Excel/PDF/图表产出提供聊天下载链接。
- SSE 心跳、LLM 流看门狗、有限缓冲、短期重放和前端异常帧容错。
- 知识文档列表的 LATERAL 子查询投影缺陷已修复，并有静态回归测试兜住同类错误。
- 代码挂载、独立 PostgreSQL Compose、宿主机日志目录和主应用/知识服务打包脚本。

#### 已缓解但必须在正式环境持续观察

- Chat 页面崩溃/卡死：已针对 SSE 空闲、异常帧、长重放和渲染异常增加保护；9.11
  追加了四层渲染压力治理（O(N)+引用稳定的工具消息合并、SSE 字节节流、热路径
  零分配、工具卡解析缓存），针对长任务工具执行期间（尤其大文件参数流式生成）
  的渐进卡顿与 admin 整页卡死。尚未取得正式浏览器 F12 Console
  堆栈，不能宣称所有浏览器/WebView 卡死都已根治；正式环境需用 admin 账号执行
  复杂任务（触发大量工具调用与大文件创建）复测验证。
- 长会话和大量工具输出：Scroll Context、think-fold 和有界缓冲已降低内存压力，
  仍需用真实模型、真实大文件和 8–10 人并发做长时间压测。
- MinerU、视觉模型和 LightRAG：离线模型预置及降级路径已完成，但模型服务本身的
  显存、并发队列和响应稳定性仍决定最终处理速度。

#### 明确尚未解决或不在当前范围

- 页面崩溃的最终浏览器级根因定位（需要正式环境浏览器 Console、Network 和崩溃时刻
  的服务日志配对）。
- 跨多台应用实例的共享任务队列和共享实时重放；当前 TaskTracker 以单主应用实例
  为边界，横向扩容前需要 Redis/数据库任务协调。
- 传统企业业务系统的细粒度数据权限；当前数据分析隔离的是汇智账号和其独立数据库，
  外部 Oracle/MySQL 等系统的业务行级权限仍由外部系统负责。
- 旧版第三方 API/iframe/回调方案已撤回，不能按历史文档重新启用。

### F. 故障排查所需信息

遇到“页面崩溃、回答消失、容器重启”时，不要只截 `docker logs`。应同时保留：

1. 宿主机日志目录中的 `qwenpaw.log`、`supervisor-app.stderr.log` 和
   `supervisor-app.stdout.log`；
2. 浏览器 F12 Console 的红色错误和 Network 中 `/api/console/chat` 的最后响应；
3. 崩溃前后的时间、账号、会话名称、是否上传文件、是否正在调用工具；
4. `docker compose ps`、容器退出码和最近一次启动命令。

没有浏览器 Console 时，只能确认后端是否断流，无法证明 React/AgentScope SDK 是否
在前端渲染阶段抛错。

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
- **多租户隔离**：每个用户独立 PG 数据库（用户名映射持久化），数据互不可见。
- **大文件支持**：单文件最大 10GB，上传和导入全程流式处理，不占满内存。
- 导入任务支持处理时长展示和失败原因行展开查看。

### 2.6B 知识空间改进

- 知识空间列表支持批量删除。
- 文档上传后自动显示处理状态（已入库/处理中/失败）。
- 支持文档预览（PDF、DOCX 等格式）。
- 知识空间菜单图标改为书本图标（`BookOpen`），与数据分析图标区分。
- 文档解析优先使用 MinerU（PDF/DOCX 布局解析），MinerU 不可用时降级到 python-docx / pdfplumber。
- 知识服务 Worker 容器已安装 MinerU + torch，支持高质量文档解析；离线镜像可预置 MinerU 全量模型（`PRELOAD_MINERU_MODELS=1`）。
- **文档级联删除清理**：删除文档时同时清理关联的实体、关系和 LightRAG 图谱数据，避免残留。
- **处理时间追踪**：文档上传和处理时间记录 `started_at`，前端显示处理时长。
- **上传就绪检查**：视觉/Embedding 模型未就绪时禁止上传并提示原因；同文件秒传去重（SHA-256）。
- **服务端分页**：文档列表支持分页（20/50/100）、状态筛选和全列排序。
- **账号严格隔离**：知识空间按账号私有，admin 也只能查看自己的空间和文档。
- **错误信息展示**：失败文档的错误详情在详情面板中显示（红色高亮）。

### 2.6C 聊天界面优化

- 移除聊天输入框中的「默认」循环模式选择器（仅管理员可见）。
- 移除聊天输入框中的「Agent 默认」工作区选择器（仅管理员可见）。
- 移除侧边栏底部的设置齿轮图标（仅管理员可见）。
- 移除聊天右上角的「打开工作区」和「正常/宽屏切换」按钮。
- 新对话默认名称改为中文「新对话」。
- Token 消耗页面图表图例改为中文（提示词元/补全词元/总词元）。
- **聊天文件自动解析**：上传文档文件（PDF/DOCX/TXT/MD/CSV/XLSX/XLS/JSON）时，自动调用知识服务解析，将提取的文本内容注入到消息中，无需手动操作。
- **DOCX 生成工具**：新增 `generate_docx` 工具，将 Markdown 内容转换为 DOCX 文件，返回 `file://` 链接供下载。

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

#### 3.2.1 全量镜像（单镜像多服务）

**所有服务共用一个镜像**，各自用不同命令启动：

| 服务 | 启动命令 | 端口 |
| --- | --- | --- |
| 汇智小助手 | `/entrypoint.sh`（Supervisor） | 8088 |
| 知识服务 API | `uvicorn app.main:app` | 18100 |
| 知识服务 Worker | `python -m app.worker` | — |
| LightRAG | `python -m lightrag.api.lightrag_server` | 9621 |

镜像命名 `huizhi-runtime:2.2.1-arm64` / `huizhi-runtime:2.2.1-amd64`，约 20GB
（zstd 导出后约 6.8GB），内置 MinerU 模型（4.6GB）与 CPU 版 torch。

**为什么合并成一个镜像**：小助手与知识服务的依赖层高度重叠，分成两个镜像会让这些层
在导出包里各存一份，白白多占本机与服务器空间；合并后所有服务共享依赖层。

**为什么用 CPU 版 torch**：默认 PyPI 的 torch 会拖入约 4.4GB 的 CUDA 运行时，而无 GPU
部署完全用不到。注意**直接卸载这些包会让 torch 崩溃**（`libcublasLt` 在导入时即加载，
实测报 `ValueError: libcublasLt.so.*[0-9] not found`），必须改为安装 `+cpu` 版本；
阿里云镜像提供官方 CPU wheel，且该目录是扁平结构，需用精确 URL 下载而不是当索引使用
（`--index-url` 无效、`--find-links` 慢到不可用）。

**构建与导出**：

```bash
# 底座（每个架构各一份，与全量镜像共享层）
bash scripts/docker_build_base.sh py312-base:2.0.2-arm64
PLATFORM=linux/amd64 bash scripts/docker_build_base.sh py312-base:2.0.2-amd64

# 全量镜像（会自动选对应的底座）
bash scripts/docker_build_allinone.sh huizhi-runtime:2.2.1-arm64
PLATFORM=linux/amd64 bash scripts/docker_build_allinone.sh huizhi-runtime:2.2.1-amd64

# 导出（默认 zstd；gzip 在此体积下又慢又不划算）
bash scripts/docker_export_image.sh huizhi-runtime:2.2.1-arm64
```

**构建环境注意**：本机为 arm64 时 amd64 走 Rosetta 模拟，实测约慢 2.1 倍，全量镜像
构建需数小时；有条件应在目标架构机器上构建。Dockerfile 内的 `RUN` 由 `/bin/sh`（dash）
执行，**不支持 `${var//pat/rep}` 这类 bash 专有替换**，写了会静默报 `Bad substitution`。

**镜像维护的最小化原则**（正式环境已清空过一次镜像，务必遵守）：

- 只保留 `huizhi-runtime:*`、`py312-base:*` 和运行时必需的 `postgres:17-alpine`；
  底座与全量镜像共享层，删底座 tag 并不释放空间，因此保留以便快速重建。
- 构建后执行 `docker builder prune -af` 清理构建缓存（本项目一次构建可产生 30GB+ 缓存）。
- 不要在容器里 `docker commit` 固化改动：所有服务共享一个镜像，从一个容器 commit 会
  覆盖其他服务的代码。依赖变化请重建镜像，代码变化走挂载更新。

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
- 改动：`MAX_ATTEMPTS = 4`，包含首次执行在内最多 4 次；每次失败后延迟 1 小时，
  即初次失败后最多再重试 3 次。超过次数后保持失败状态，并保留可下载的日志片段。
- 效果：短暂的模型、网络或资源故障无需人工干预；持续失败不会无限重试。

涉及文件：
- `knowledge-service/app/providers.py`：`extract_facts()` 新增 `_extract_batch()` +
  `ThreadPoolExecutor(max_workers=3)` 并行抽取；`embed()` 改为 32 块/批调用
- `knowledge-service/app/store.py`：`search_by_embedding()` 改用 numpy 向量化；
  新增 `_cjk_bigrams()` + `ConnectionPool`
- `knowledge-service/app/worker.py`：失败任务重试和日志片段保存，
  `MAX_ATTEMPTS = 4`

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

### 9.7 聊天文件自动解析与 DOCX 生成

**背景**：用户在聊天中上传文档（PDF/DOCX/TXT/MD/CSV 等）时，文件仅存储在磁盘，模型通过工具读取。大文档或格式复杂的文档（如含表格、公式的 PDF）直接读取效果差，且无法利用知识服务的 MinerU 解析能力。

**改动 1：知识服务 Parse API**
- 新增 `POST /v1/documents/parse` 接口，接受文件上传，返回解析后的纯文本。
- 支持格式：PDF、DOC、DOCX、TXT、MD、CSV、XLSX、XLS、JSON。
- 内部复用知识服务的 `extract_text_from_file()` 解析管线（MinerU + LibreOffice + SimpleReader）。
- 安全限制：单文件最大 50MB，超时 60 秒。

涉及文件：
- `knowledge-service/app/main.py`：新增 `POST /v1/documents/parse` 端点
- `knowledge-service/app/parsers.py`：新增 `extract_text_from_file()` 公共函数

**改动 2：MediaProcessHook 集成**
- 修改 `process_file_and_media_blocks_in_message()`，对文档类文件（非图片/音频/视频）自动调用 Parse API。
- 解析成功后将文本内容注入到消息中，格式为：
  ```
  用户上传了文件：example.pdf
  --- 文件内容开始 ---
  [解析后的文本]
  --- 文件内容结束 ---
  ```
- 解析失败时降级到原有逻辑（仅记录文件路径）。
- 支持语言感知的分隔符（中文/英文）。

涉及文件：
- `src/qwenpaw/agents/utils/message_processing.py`：新增 `_parse_file_via_knowledge()` 函数，修改 `process_file_and_media_blocks_in_message()`

**改动 3：DOCX 生成工具**
- 新增 `generate_docx` 工具，将 Markdown 内容转换为 DOCX 文件。
- 支持：标题（H1-H4）、列表（有序/无序）、表格、水平分割线。
- 字体回退链：Microsoft YaHei → DejaVu Sans → Noto Sans CJK SC → Arial Unicode MS（跨平台 CJK 支持）。
- 保存到聊天媒体目录，返回 `file://` 链接，复用现有 `send_file` 工具投递。
- 前端零改动：复用现有 `<Files>` 下载卡片。

涉及文件：
- `src/qwenpaw/agents/tools/generate_docx.py`：新增工具
- `src/qwenpaw/agents/tools/__init__.py`：注册工具

**工作流程**：
```
用户上传 PDF → MediaProcessHook 检测文件类型
    ↓
调用知识服务 POST /v1/documents/parse
    ↓
MinerU 解析 PDF → 返回纯文本
    ↓
文本注入到消息 → 模型阅读解析后的内容
    ↓
模型回答问题或生成 DOCX 报告
```

### 9.8 知识文档级联删除清理

**背景**：删除文档时仅删除了 documents 表记录，关联的实体（entities）、关系（relations）和 LightRAG 图谱数据未清理，导致数据库残留和图谱噪声。

**改动**：
- `delete_document()` 返回值从 `bool` 改为 `str`（`"deleted"/"not_found"/"processing"`），区分处理中状态避免并发冲突。
- 新增实体清理：删除文档后检查 mention 计数，零 mention 的孤立实体自动删除。
- 新增关系清理：删除孤立的 evidence 为 NULL 的关系，以及引用已删除实体的关系。
- 新增 LightRAG 清理：调用 `DELETE /documents/delete_document` 删除图谱数据，409 冲突（忙碌）时跳过。
- 新增 `started_at` 字段：文档上传时记录开始处理时间，前端可显示处理时长。
- 并发保护：拒绝删除 `processing` 状态的文档（返回 409），避免竞态条件。

涉及文件：
- `knowledge-service/app/store.py`：`delete_document()` 重写，新增 `started_at` 迁移
- `knowledge-service/app/lightrag_bridge.py`：新增 `delete_document()` 方法
- `knowledge-service/app/main.py`：删除端点增加 409 处理和 LightRAG 清理

### 9.9 知识空间与数据分析 UI 统一

**背景**：知识空间和数据分析页面的状态展示、时间信息、排序和筛选功能不一致，用户体验割裂。

**改动**：
- 两个页面都新增 `started_at` 时间字段，显示上传/处理/完成时间。
- 所有列支持排序（点击表头切换升序/降序），状态列默认升序（pending 在前）。
- 数据分析页面新增状态筛选下拉框（全部/处理中/已完成/失败/部分失败）。
- 失败文档的错误详情在详情面板中显示（红色 `<pre>` 块）。
- 处理时长自动计算：`completed_at - started_at`，显示为秒数。

涉及文件：
- `console/src/api/modules/knowledge.ts`：`KnowledgeDocumentStats` 类型新增 `started_at`、`error`
- `console/src/api/modules/dataAnalysis.ts`：`DataImportJob` 类型新增 `started_at`
- `console/src/pages/Knowledge/index.tsx`：StatsDrawer 时间展示、排序、错误详情
- `console/src/pages/DataAnalysis/index.tsx`：状态列、筛选、排序、错误详情

### 9.10 第二轮集中改造：离线部署加固、LLM 流稳定性、多租户隔离与部署工程化

本节汇总知识服务、主应用后端、前端和部署脚本的大批量改动（71 文件，+2500 行）。

#### 9.10.1 知识服务：离线可用与大文件能力

**离线部署加固**（生产服务器无外网）：
- Dockerfile 新增 `PRELOAD_MINERU_MODELS=1` 构建参数：构建时把 MinerU pipeline+VLM 全量模型打入镜像，运行时 `MINERU_MODEL_SOURCE=local` 本地加载。
- 新增 `KNOWLEDGE_OFFLINE` 配置项；`auto` 模式下 MinerU 失败自动回退 pdfplumber/python-docx（旧版直接整单失败）；扫描版 PDF / 离线 .doc 给出明确中文报错。

**模型配置拆分与上传就绪检查**：
- Embedding 模型不再自动猜测，必须显式配置 `EMBEDDING_BASE_URL` + `EMBEDDING_MODEL`；`.env` 拆分为 `CHAT_*` / `EMBEDDING_*` 两组（`PROVIDER_*` 向后兼容）。
- 新增 `upload_ready` 健康检查：仅 embedding 未就绪时 `/v1/documents/upload` 直接 409 并说明缺什么。该检查会实际请求 embedding 端点验证地址、密钥和模型名，避免生成没有语义索引的文档。视觉模型不参与上传前判断：Worker 对图片做最佳努力解析，当前模型支持多模态则写入图片描述，不支持则自动跳过，文字、表格和文档主体仍可上传和索引。

**大文件与图片**：
- `KNOWLEDGE_MAX_FILE_MB` 默认提到 10240（10GB）。
- 轻量解析路径下提取 DOCX（zip word/media/）和 PDF（PyMuPDF）内嵌图片，转 base64 data URI 随解析结果返回。

**去重与分页**：
- 上传流式计算 SHA-256，`documents` 表新增 `content_hash`；同账号同空间相同文件直接返回已有文档（`duplicate: true`），不落盘不建任务。
- 文档列表按 `(owner, space, filename, hash)` 去重只显示最新一条。
- 新增 `list_user_documents_page`：按状态过滤 + 分页（`{items, total, page, page_size}`），`/v1/documents` 响应结构相应调整。

**级联删除与账号隔离收紧**：
- `processing` 状态文档拒绝删除（409），防止与 worker 竞争。
- 删除后清理 space 内孤儿 relations/entities；LightRAG 图数据删除改为异步 best-effort。
- 知识空间查询移除 `OR is_admin` 写法：**admin 也只能看自己的空间和文档**，严格按账号私有。
- 修复 LightRAG 启动命令：`lightrag.server` → `lightrag.api.lightrag_server`（lightrag-hku 1.5.6 正确路径）。

#### 9.10.2 主应用后端：LLM 流稳定性与工具可靠性

**LLM 流式看门狗**（`retry_chat_model.py`，+502 行，解决流卡死请求永久挂起）：
- 双阶段空闲超时：首内容超时（`LLM_STREAM_FIRST_CONTENT_TIMEOUT`）+ 稳态空闲超时（`LLM_STREAM_IDLE_TIMEOUT`），每个有内容 chunk 重置预算；空的控制帧不算进展（语义判断在新增的 `providers/stream_progress.py`）。
- 刻意不用 `asyncio.wait_for`（AgentScope 会吞 CancelledError），改为独立 task + `asyncio.wait` 强制超时。
- 有界清理：stream `aclose` 1 秒上限，不配合的转后台任务并按 `model_key` 登记；清理期间该模型新请求直接拒绝（`StreamCleanupPendingError`），避免新旧请求互相污染。
- `generate_structured_output` 补齐重试+限流路径；自定义 OpenAI 兼容端点发现模型失败改为直接抛错（不再静默返回空列表）。
- 内联媒体上限 2MB → 20MB，对齐聊天附件。

**上下文压缩新增 think-fold 阶段**（scroll manager 第 8 步）：
- 持续压力下，把"已被成功请求读取过"的 ThinkingBlock 在请求期省略（不动 history.db 原文），折叠后精确重算 token，无效则回滚；通过 formatter 的 `set_thinking_omit_ids` 实现。

**工具调用输入类型修复**（新模块 `agents/utils/tool_call_coerce.py`）：
- 模型对声明为 `type: string` 的参数输出裸数字/布尔（如 `"assetInfo": 1.000001`）导致 schema 校验失败、工具必败——按 schema 遍历强转，`_FloatLiteral` 保留原始字面文本（`1.000000` 不会变 `"1.0"`），合法值永不改动。
- 顺带修复 `_execute_tool_call` 签名 bug（缺 `kept_rules` 参数，并发路径每次 TypeError）。

**数据分析多租户隔离与大文件导入**：
- 每用户独立 PG 数据库：`ensure_user_database()` 按用户名建库（非规范化用户名加 sha256 短哈希防碰撞），映射持久化在 `tb_hzxzh_user_database`；不再读外部数据源连接。
- 导入流式化：CSV `chunksize=10000` 分块、XLSX read-only 按批产出；上传 8MB chunk 落盘、单文件上限 10GB，失败清理临时文件——多 GB 导入不再占满内存。
- 元数据表统一 `tb_hzxzh_*` 常量命名；建表主键改 `BIGSERIAL`（去掉 pgcrypto 依赖）。

**Agent 归属权限收紧**：
- 新增 `user_agent_ids` / `require_owned_agent_id`：admin 也只能操作自己名下的 agent（访问他人 agent 返回 404），配套单元测试 `test_user_space_agent_ownership.py`。

**其他**：
- `generate_chart` 工具（bar/pie/line/scatter/area/horizontal_bar，matplotlib 出 PNG，配合 send_file 投递）。
- 流式事件累积从字符串 `+=` 改为分段列表 join（`runtime/envelope.py`），消除 O(n²) 拼接。
- 文件读取对"单行超长"给出 artifact 支撑的有界预览（旧逻辑会死循环或静默跳过）。
- 技能 frontmatter 清洗 C0 控制字符；secret 解密失败日志按密文指纹去重；`QWENPAW_LOG_DIR` 可重定向日志目录。

#### 9.10.3 前端

- **工具调用卡死修复**（useToolCallControl）：新建会话 session 尚不存在时 `getInfo` 404 → 置 missing 标志停止重试/轮询/卸载注册；`request.ts` 给错误对象附加 `status` 字段。
- **流式解析容错**（Chat responseParser）：缺 `msg_id` 的心跳帧/`turn_usage`/限流帧统一转 heartbeat，修复发送框转圈卡死。
- **附件限制改为累计 20MB**：按已选附件总大小判断（原来只限单文件），6 语言文案同步。
- **知识库页面**：服务端分页（20/50/100）；上传放宽到 10GB + `upload_ready` 就绪检查；修复多选文件重复上传 30 次的 bug（`uploadBatchGuard`）；新增解析分块预览（DOCX 失败降级、.doc 直接用分块预览）。
- **数据分析页面**：全列排序、导入任务处理时长列、失败任务行展开看错误详情。
- Agent 运行配置 API 改为按 agent 作用域路径（`/agents/{id}/workspace/running-config`）。

#### 9.10.4 部署与打包体系

**代码挂载（code-mount）模式成为主部署方式**——基座镜像不变，代码 volume 只读挂载，更新=替换目录+重启：

| 脚本 | 用途 |
|------|------|
| `scripts/package_merge_updates.sh` | **日常更新统一用这个**：一条命令产出主应用 + 知识服务 + deploy 的合并包 |
| `scripts/package_code_update.sh` | 仅主应用代码包（src + console/dist + 插件），紧急热修用 |
| `scripts/package_knowledge_code_update.sh` | 仅知识服务代码包，紧急热修用 |
| `scripts/package_assistant_update.sh` | 主应用组合包（代码 + deploy 层），不含知识服务 |
| `scripts/docker_build_knowledge.sh` | 构建 arm64 知识服务镜像（可选预装 MinerU 模型），全新部署用 |

**日常更新不要再分包**：主应用与知识服务统一打成一个
`huizhi-code-update-YYYYMMDD.tar.gz` 一起上线，避免两个包分别部署造成版本错配。
`package_merge_updates.sh` 已改为自动调用两个服务的打包脚本（含前端构建），不需要
先手动准备分件包，直接执行即可：

```bash
bash scripts/package_merge_updates.sh
```

**包名约定**：一律用「前缀 + 日期」，即 `huizhi-code-update-YYYYMMDD.tar.gz`（合并包，
日常用）、`huizhi-knowledge-code-YYYYMMDD.tar.gz`、`huizhi-assistant-code-YYYYMMDD.tar.gz`、
`huizhi-assistant-app-code-YYYYMMDD.tar.gz`（后三者仅紧急热修单个服务时用）。
不要加 git SHA、架构后缀或 `-schema-selfheal` 这类描述性尾巴；包内容是架构无关的
Python 代码和前端产物，架构差异体现在 compose 与镜像里。同名包在同一天重复打包会
覆盖，需要留档时显式传入自定义文件名（脚本第一个参数），校验值始终写在同名
`.sha256` 里。该约定同时写入仓库根目录的 `AGENTS.md`，供后续会话直接读取；该文件
按 `.gitignore` 规则不纳入版本管理（沿用上游做法）。

**日志持久化**：
- 应用日志写入 `QWENPAW_LOG_DIR`；Supervisor 和其子进程日志也会写入同一目录，避免仅依赖容量有限的 `docker logs`。
- 老基座镜像没有新模板时，`code-mount-entrypoint.sh` 会在启动时改写 Supervisor 配置，使日志仍落入挂载目录。

**正式环境日志落盘**：
- `deploy/docker-compose.code-mount.arm64.yml` 将容器 `/app/working/logs` 挂载至宿主机 `${QWENPAW_HOST_LOG_DIR:-./data/logs}`。应用日志为 `qwenpaw.log`，Supervisor 与各子进程日志为 `supervisord.log`、`supervisor-app.stdout.log` 和 `supervisor-app.stderr.log`。
- 每个 Supervisor 文件单文件上限 20MB，保留 5 个历史文件；可避免异常高频输出占满磁盘。`QWENPAW_HOST_LOG_DIR` 只表示宿主机目录，不要设置为容器内路径。

**Compose 拆分**：
- `docker-compose.code-mount.arm64.yml`：ARM64 生产部署（不含 PG，数据库通过独立 Compose 或外部 PG 提供），api/worker 只读挂载宿主机 `./app`。
- `knowledge-service/docker-compose.postgres.yml`：独立 PG17 容器（仅绑定 127.0.0.1），应先单独启动，再启动知识服务 Compose。

### 9.11 Chat 长任务卡死修复：渲染压力四层治理

**问题**：执行复杂任务时，某个工具动作长时间转圈（如"创建 文件 处理中"——
模型正在把大文件内容作为工具参数流式生成）期间，页面越来越卡，交互明显迟滞；
admin 账号最终演变为整页卡死——连浏览器标签页都无法关闭，只能杀掉整个浏览器。
普通用户同样会卡顿（"干什么都反应慢一拍"），只是授权工具少、单次任务工具调用
与大参数更少，未达完全卡死阈值。v1.1.12 因未使用 AgentScopeRuntimeWebUI 的
响应卡渲染链路，无此问题。

**根因**（四个因素叠加，随任务时长单调恶化）：

1. **SDK 的 `AgentScopeRuntimeResponseBuilder.mergeToolMessages` 为 O(N²)**：
   每个 tool output 匹配时对全部已有消息做 `resMessages.map()` 线性扫描。N 次
   工具调用 → N²/2 次遍历，且每次渲染（节流后仍每 80ms 一次）都重新执行。
2. **SDK 的 `useChatRequest` 对每个 SSE 帧调用 `updateMessage`**：活跃流式输出
   时 20–50 次/秒，每次都触发 React 状态更新；字节层面无任何节流。
3. **`responseParser` 每帧新建 `INTERNAL_INJECTION_NAMES` Set**：长任务数千帧
   = 数千个临时对象，放大 GC 压力。
4. **渲染链破坏对象身份，导致已完成工具卡每 tick 重复 JSON 解析**（9.15 补充
   定位，"创建文件处理中"转圈越久越卡的直接原因）：响应 builder 每个流式 delta
   都产出新 output 数组 → merge 每次重跑并新建全部已合并工具消息对象 → SDK
   `Tool` 组件的 `React.memo` 因 `data` 引用变化失效 → 所有历史工具卡重渲染 →
   `v1Adapter.parseV1Props` 对每个已完成卡 `JSON.parse(完整参数)`、
   `ToolCardShell` 再 `JSON.stringify` 美化打印参数。创建大文件时模型把文件内容
   作为工具参数逐 token 流式生成，历史参数随任务越积越大，每 tick 的 JSON
   解析/序列化成本线性增长（每 tick ≈ 2× 全部参数体积），最终主线程饱和。

当单次渲染耗时超过节流间隔后，渲染任务排队堆积，主线程被完全占满，浏览器失去
响应（无法关闭标签页）。普通用户同样受第 1/2/4 项影响，表现为"页面特别卡、
干什么都反应慢一拍"，只是工具调用数量少、未达完全卡死阈值。

**修复**（四层叠加）：

| 层 | 修改 | 效果 |
|---|---|---|
| 渲染算法 | `HostBubbles.tsx` 新增 `mergeToolMessagesLinear`（Map 索引 O(1) 匹配 + WeakMap 合并结果缓存）替换 SDK 的 O(N²) 实现；渲染节流 80ms → 150ms | 每次渲染成本与工具数线性相关；已完成工具卡跨 tick 保持引用稳定，`React.memo` 生效 |
| SSE 传输 | `turnUsage.ts` 新增 `createSseThrottleStream`：原始字节按 50ms 批量透传（上限 256KB/批），Completed/Failed/Canceled 终端帧立即透传 | 让 React 18 automatic batching 合并一批内的多次 `updateMessage` 为一次 re-render |
| 热路径分配 | `index.tsx` 将 `INTERNAL_INJECTION_NAMES` 与知识注入前缀常量提到模块级 | 消除每帧临时对象分配 |
| 卡片解析 | `v1Adapter.tsx` 包裹层加 `React.memo` + 按消息对象身份缓存 `parseV1Props` 结果（WeakMap） | 已完成工具卡的 `JSON.parse(参数)` 只在消息首次出现时执行一次；流式中只有活跃卡重渲染（预览有界 4KB） |

**验证**：
- `npx tsc --noEmit` 通过。
- `src/pages/Chat/`、`src/components/Chat/`、`src/hooks/` 共 42 个测试文件
  312 个测试通过（含 `HostBubbles.test.ts` 14 个用例：合并、乱序、交叉调用、
  50 次工具调用性能 <10ms、跨 tick 合并结果引用稳定性等；`v1Adapter.test.tsx`
  5 个用例：参数 JSON.parse 单次执行、React.memo 身份跳过、流式 delta 预览）。
- `RichFileReferenceInput.test.tsx` 的 2 个失败为既有问题（文件引用输入组件
  chip 渲染），与本修复无关。

**遗留观察项**（非本次根因，待后续优化）：
- `turnUsage.ts` 的 `schedulePatchLastResponseCardUsage` setTimeout 重试链
  （最多 40×50ms）在组件 unmount 后不清理，且每次重试走 `ReactDOM.flushSync`。
- SDK 内部消息列表无 `maxMessages` 裁剪（有分页渲染兜底，DOM 不失控）。
- 反向消息列表 wheel 拦截 `passive: false` 在超长消息列表下的 DOM 遍历成本。

### 9.12 知识服务文档列表查询修复：LATERAL 子查询漏投影

**问题**（正式环境 2026-09-18 09:47）：知识空间文档列表接口 500，
`psycopg.errors.UndefinedColumn: column j.stage does not exist`，堆栈为
`main.py` 的 `list_all_documents` → `store.py` 的 `list_user_documents_page`；
前端知识空间文档列表打不开（`GET /v1/documents`）。

**根因**：`list_user_documents` 和 `list_user_documents_page` 的外层 select 引用
`j.stage AS job_stage`，但配套的 `LEFT JOIN LATERAL (...)` 子查询只投影了
`id, status, attempts, error`，没有 `stage`。报错位置在外层那一行（`LINE 13`），
与「表缺列」的表现不同：如果真是表缺列，指针会落在子查询内部。

**为什么先前的 schema 自愈没有生效**：同日上午已经给 `_init_db` 补上
`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS stage TEXT` 并打包上线，但子查询不投影
`stage` 时，外层引用在任何 schema 下都非法——迁移再正确也修不好这个查询。这就是
补列之后错误依旧复现的原因。诊断时容易误判成纯 schema 漂移，实际是 SQL 写错了。

**修复**：两处 LATERAL 子查询改为 `SELECT id, status, stage, attempts, error FROM jobs`。
另一处 `list_documents_internal`、`list_documents` 只引用 `id/status/attempts/error`，
不受影响。

**验证**：

- 用 `pg_dump --schema-only` 把本地旧库（`jobs` 仅 7 列、无 `stage`）克隆成临时库，
  在同一库上做对照：修复前 `FAIL: column j.stage does not exist`（与线上完全一致），
  修复后返回 `OK`，`job_stage` 正确往返为 `embedding`。
- schema 漂移审计：旧库缺 `documents.content_hash`/`processing_stage`、
  `jobs.log_excerpt`/`next_retry_at`/`stage`，全部有对应迁移；`worker_heartbeats`
  是整张新表，由 `CREATE TABLE IF NOT EXISTS` 建立，不需要单独迁移。
- 新增静态回归测试 `tests/unit/knowledge_service/test_store_lateral_job_projection.py`：
  断言「外层引用的每个 `j.*` 列都被 LATERAL 子查询投影」，不需要数据库；已验证把修复
  回退后该测试会同时报出两处违规。

### 9.13 Chat 驱动的入库：智能体代为建表/导入与上传知识空间

**背景**：数据分析、知识空间、定时任务原本都要求用户先理解自己的数据，再去对应页面
手动建表、上传、配置 cron。真实使用中大多数用户的数据是不规整、零散的，用户自己也
说不清，只能给个大概背景然后把文件丢进聊天框。定时任务已经有「懂 crontab 自己配、
不懂就让智能体配」的 skill + CLI 路径，本次把同一个模式补到数据分析与知识空间。

**原先为什么做不到**（已核实）：agent 侧只有 `list_data_tables`、`describe_data_table`、
`query_data_table` 三个只读工具（`query_data_table` 还硬拦非 SELECT/WITH）；建表和导入
只有给控制台前端用的 HTTP 接口，且导入只接受浏览器 `UploadFile`。`list_data_tables`
在没表时的原话就是「请先通过「数据分析」菜单创建数据表并导入数据」，即设计上是手工流程。

**新增四个写工具**：

| 工具 | 作用 |
| --- | --- |
| `create_data_table` | 用一句中文描述建表；不需要列定义 |
| `import_data_table` | 把用户的 CSV/XLSX/XLS 导入指定表，返回成功行数或失败原因 |
| `create_knowledge_space` | 新建知识空间；同名已存在则复用，不重复建 |
| `upload_knowledge_document` | 把用户的文件上传到知识空间（留空则用默认空间） |

**触发边界**（避免重蹈此前的误路由）：查询仍严格由精确短语「通过数据分析」门控，未
改动。写工具只在**两条同时成立**时使用：(1) 本轮消息带了表格附件或用户明确指向工作区里
的表格文件；(2) 用户表达了入库意图（建表/入库/存起来/以后能查）。只让智能体「看看
数据」时不建表，而是在回答后主动提议一次「要不要建成数据表，以后可以直接查询」。

**聊天附件的来源**：附件落在 `<工作区>/media`（`CONSOLE_MEDIA_DIR` 可覆盖），磁盘文件名
是 `<32 位 uuid>_<原文件名>`。工具按文件名解析（模糊匹配最新的一个），对外展示和导入
任务名会剥掉 uuid 前缀。边界控制：

- 绝对路径必须落在此账号允许的根内（工作区 / 媒体目录 / 账号根），用 `resolve()` 后再做
  包含性校验，符号链接指向外部会被拒绝；
- 扩展名白名单、10GB 上限、传统 XLS 512MB 上限，全部复用 `data_tables/store.py` 的常量
  （这些常量已从 `import_api` 挪到 `store.py`，避免工具为了一个路径常量把 pandas 拉进来）；
- 只认调用账号自己的表（`owner_username` 精确匹配，不做 admin 越权），
  与「数据分析每用户独立 PG 库」的隔离方向一致。

**导入不消耗聊天附件**：`data_tables/importer.py` 导入成功后会删除源文件，所以工具先把
附件**拷贝**到 `UPLOAD_DIR/<uuid>/` 再交给导入任务，用户的原附件保留。导入是后台任务，
工具做有界等待（约 45 秒），小文件直接回报行数，大文件回报任务 ID 与当前状态。

**权限说明（重要）**：本部署把工具审批硬写死为 `OFF`（`governance/tool_adapter.py` 在读取
任何配置前就直接返回），策略引擎不会被调用，因此写工具的 `default_policy="ask"` 在当前
版本**不构成任何拦截**，只为审批机制恢复后留下正确的种子。真正的门禁是工具函数体内的
账号归属校验与路径包含性校验，以及按账号的工具开关。新工具默认 `enabled_by_default=True`，
会经 `ToolsConfig._merge_default_tools` 自动补进已有和新建用户的 agent。

**验证**：

- 新增 42 个测试：`test_user_files.py`（11，含越权路径、符号链接逃逸、`../` 穿越、扩展名）、
  `test_data_table_write_tools.py`（10，含表归属、附件不被消耗、部分失败、超时回报 job id）、
  `test_knowledge_write_tools.py`（9，含就绪检查 409、重复上传、同名空间复用）、
  `test_table_api.py`（6，建表核心允许空列、按 owner 隔离、无 admin 越权）、
  `test_prompt_contributors.py`（+4，锁定触发边界与「通过数据分析」门控未被放宽）、
  `test_unified_tool_registration.py`（+2，新工具的策略名与 target_param 登记）。
- `tests/unit/governance` 与 `tests/unit/agents/tools` 全量回归通过；与干净 HEAD 逐项对比
  失败清单，本次改动没有引入新的失败（既有失败均为与本功能无关的历史问题）。
- 端到端实测（`scripts/verify_chat_ingest.py`，跑在真实 PostgreSQL 上）：按聊天附件的
  真实命名方式造一个 `销售数据.csv` → `create_data_table` 建表 → `import_data_table`
  导入 → `query_data_table` 得到 3 行；同时验证原附件未被消耗、工作区外路径被拒、
  跨账号表不可达。脚本自带清理，验证后无残留表/库/任务。
- 实测确认模型的参数 schema 由函数签名生成（`FunctionTool.input_schema`），四个工具的
  参数与必填项正确；同族只读工具里的 `parameters=` 元数据其实从未被读取。
- `data_tables/store.py` 的 `UPLOAD_DIR` 是容器内路径（`/app/working/data_uploads`），
  在宿主机直接跑工具时需重定向；容器部署下无需处理。

**已知边界**：聊天附件上限 20MB 且解析文本只截取首尾，大表仍建议走数据分析页面上传
（支持到 10GB）；定时任务本轮未动，仍走 skill + CLI，其 slash 命令固定指向 `default`
工作区的多用户隐患待单独处理。

### 9.14 账号用量统计：管理员查看各账号词元消耗

**背景**：现有「词元消耗」页面是全局视图，只能看到整体用量，无法回答「谁用得多」。
本功能新增管理员专属的按账号统计页，可筛选日期与账号、柱状图对比、并显示所选范围的合计。

**先决问题：用量数据原本没有账号维度**。这是本次最关键的一点——原有的
`token_usage.json` 是全局单文件，结构为「日期 → `provider:model` → 计数」，采集端
`model_wrapper._record_usage` 只填 provider/model/token/日期，**没有用户字段**。
因此无法从历史数据反推归属，只能从本次改动上线后开始按账号累积。

**采集**：`_UsageEvent` 增加 `user_id`；新增 `_resolve_usage_user_id()` 优先读请求
ContextVar，定时任务与后台运行没有请求上下文时回退到 agent profile 的
`owner_username`——否则定时任务的消耗会全部落到「未归属」。

**存储（关键取舍）**：账号细分**内嵌在现有条目内部**（`by_user`），而不是把账号加成
新的顶层维度：

```
{ "2026-09-18": { "prov:model": {
    prompt_tokens: .., completion_tokens: .., call_count: ..,   ← 全局计数，原样不变
    by_user: { "alice": { prompt_tokens: .., ... } },
} } }
```

这样做的原因有两个：一是现有全局页面的读取路径**完全不受影响**（顶层计数含义不变），
二是避免「同一模型按账号拆成多行」后全局页面把同一模型重复统计。代价是文件更大。

**历史数据与实际值对账**：一个条目可能同时含有归属前的旧量和归属后的新量，所以
「未归属」不是简单地按「有没有 `by_user`」判断，而是逐条目用
`全局值 − 已归属之和` 推算（`_sum_user_entries`）。这一点由测试锁定：如果按
「有 by_user 就算全部归属」处理，混合条目里的旧量会被静默漏掉。

**接口**：`GET /token-usage/accounts?start_date&end_date&accounts=`（管理员专属）。
`accounts` 省略时返回该时间段内所有有消耗的账号；返回 `by_user`、`by_date` 与
`unattributed`。权限用与用户管理接口相同的 `require_admin_access`，非管理员返回 403、
未登录返回 401，**在聚合之前就拒绝**。

**前端**：新增 `pages/Settings/AccountUsage`（菜单与路由 id 为 `core.account-usage`，
已加入 `ADMIN_ONLY_MENU_IDS`，普通用户看不到入口）。柱状图用 `@ant-design/plots` 的
`Column`（依赖已有，未新增库），支持五种指标切换（总词元/提示/补全/调用次数/
提示+补全堆叠）。筛选行为：日期范围 + 账号多选（**默认全选**，去掉某些账号即只显示
剩下的）。页面始终拉取全量账号数据再做前端筛选，因此切换账号是即时的，且选择器能
列出全部账号。

**合计口径**：汇总卡片由**当前可见行**求和得出，而非直接采用服务端的总数——这样
去掉某个账号后，卡片、柱状图、明细表三者始终一致。这是与「也要有一个总的统计」这条
需求一致的取舍。

**数据整理逻辑抽成纯函数**（`AccountUsage/aggregate.ts`）以便测试覆盖
「合计必须与图表一致」这条规则。测试：后端 `test_account_usage.py` 8 个用例（分组、
全局计数不变、混合条目的未归属推算、账号白名单、按日/按模型细分、越界日期、脏数据）
+ `test_account_usage_router.py` 5 个（401/403/正常聚合/默认全量/日期反序）；
前端 `aggregate.test.ts` 13 个 + `AccountUsagePage.test.tsx` 5 个 + API 3 个。

**验证**：把端点里的 `_require_admin(request)` 临时删掉后，两个权限用例精确失败
（`DID NOT RAISE`），确认测试真的守住了越权；与干净 HEAD 逐项对比失败清单，本次改动
没有引入新的失败（既有的 20 余项失败与本功能无关，前端失败集中在
`ThemeContext`/`useAgentConfig` 等既有测试）。

**已知边界**：历史用量无法归属，统一显示为「未归属（历史数据）」；统计仅含词元消耗
与调用次数，不含会话数、文档数等指标（这些数据源分散在各自存储，需另做跨系统聚合）。

### 9.15 用量可见性收口：普通用户只能看到自己的用量

**背景**：用量要作为绩效考核依据，必须公开透明——每个人能看到自己的用量，管理员能看到
全部。但原有实现**三处出口都返回全局合计**，普通用户看到的是所有人的总量。

**泄露面（三处，缺一处修都不算修好）**：

| 出口 | 原状况 |
| --- | --- |
| `GET /token-usage`（汇总） | 无鉴权，函数签名里连 `request` 都没有，结构上无法识别调用者 |
| `GET /token-usage/details`（明细） | 同上，前端聚合页直接读它 |
| `get_token_usage` agent 工具 | 在聊天里问「我用了多少词元」，进程内直接读全局汇总 |

前两处的根因是签名缺 `Request`，所以**加参数是修复的前提**；第三处是独立入口，
只改 HTTP 会留下一个可从聊天绕过的口子。

此外，`user_space.py` 的 `DEFAULT_USER_MENU_IDS` 里本来就含 `core.token-usage`，
所以普通用户开箱即用能看到这个页面——页面本身留着（公开透明），但内容改为只含自己。

**授权规则集中在一处**：新增 `token_usage/visibility.py::visible_account(username)`：
管理员返回 `None`（全局视图），普通账号硬性返回自己。规则只有一份，避免 HTTP 与
agent 工具各自实现导致口径不一致。判定用 `is_admin_user()`（按角色，而不是用户名
是否字面等于 `admin`）；角色查询异常时**返回调用者自己**而不是放宽为全局——失败必须
收紧，不能放大可见范围。

**个人数字的口径（影响考核可信度）**：过滤指定账号时读取的是该账号自己的计数
（`by_user[账号]`），**不是条目总数**。因为条目总数里还含「未归属」的历史量，若按总数
计算，某个人的用量会把别人的旧账算进去。这条由
`test_manager_filter_excludes_unattributed_usage` 锁定。

**验证（回退验证）**：把三处过滤分别移除后，5 个用例精确失败（`assert 600 == 100`
等），管理员用例仍通过；恢复后 8 个全过。这证明三处都有独立覆盖，不是其中一处通过
就掩盖其余两处。另外 `tests/unit/agents/tools`、`tests/unit/token_usage` 等
369 项回归全过。

**已知边界**：历史数据已确认可不保留，管理员页面上的「未归属」行可直接视为过渡期
噪音；`by_user` 只有本次上线之后产生的数据，因此绩效统计口径应从上线日起算。

### 9.16 工具权限快照缺陷：新上线的工具被静默隐藏

**现象**：用户在聊天里要求「建表并导入数据」，智能体回答
「当前会话里 `create_data_table` / `import_data_table` 都没有暴露出来」。
工具代码本身没问题（能注册、能跑、端到端验证过），问题在**它没进模型看到的工具列表**。

**根因**：`account_tools` 是一份**快照**——管理员上次显式配置工具权限时，把当时的工具池
存了下来（`set_account_tools` 曾把池内每个工具都写成 `name in selected`）。之后新上线的
工具不在那份名单里，于是被当成「管理员主动禁用了」。两条路径都会踩到：

- `get_account_tools()` 返回「池 ∩ 已保存名单」，新工具被排除；
- `set_account_tools()` 把新工具写成 `enabled=False`，而 `list_tools` 正是靠 agent 配置里
  的 `enabled` 标志过滤的（`_resolve_config_gates`）。

第三条触发路径解释了为什么是**新建智能体**最先暴露：`routers/agents.py` 创建 agent 后会
调用 `set_account_tools(username, get_account_tools(username))` 重新套用账号权限——把陈旧
名单原样写进新 agent 的配置，于是新建的 agent 一律缺少新工具（截图里的 agent id 正是随机
后缀的新 agent）。

这是**通用缺陷**，不只影响数据分析工具：任何在一次权限保存之后新增的内置工具都会这样消失，
且没有任何日志或提示，排查成本很高。

**修复**（两侧都改，缺一不可）：

1. **读取端宽容**：新增 `account_tools_known` 记录「做这次选择时池里有哪些工具」，
   `_account_tools_from_state()` 借此区分「没出现过」与「被主动排除」——前者回落到该工具
   自身的默认值（默认启用的仍然启用），后者严格尊重。旧状态没有这个标记，无法区分，
   按宽容处理（宁可多启用一个默认开启的工具，也不要静默隐藏一个新工具——前者可见且可
   重新关闭，后者是无声故障）。
2. **存量配置自愈**：`heal_account_tool_allowlists()` 在启动时对**没有标记的账号**
   重新套用一次修正后的选择，把已被写坏成 `enabled=False` 的配置改回来，并写入标记；
   之后再跑即为空操作。只处理没有标记的账号，因此不会覆盖管理员后续的主动选择。
   单个账号失败不影响其余账号，读不到账号列表也不阻断启动。

**验证**：新增 12 个用例（新工具被启用、旧状态必须宽容的语义、有标记时严格尊重主动排除、
默认关闭的插件工具不被误启用、输出顺序、自愈幂等、只处理无标记账号、单账号失败不阻塞、
账号列表不可读不致命）。回退验证：把读取端退回旧逻辑后 3 个用例精确失败。另用真实
`state.json` 与真实 agent 配置做过一次端到端自愈（`False` → `True`，第二次运行为 0）。
`tests/unit/app`、`token_usage`、`config`、`agents` 共 2157 项回归通过，与干净 HEAD
逐项对比失败清单一致（既有失败为历史问题，另有一项 flaky 互换）。

**部署后行为**：重启主应用即自动修复所有受影响账号，无需手工重新勾选工具权限。
仅当管理员确实希望关闭某个默认开启的新工具时，才需要重新保存一次该账号的工具权限
（保存后即产生标记，后续升级不会再影响它）。

### 9.17 生成的文件预览被拒（403）：落点不在账号工作区内

**现象**：聊天里智能体生成 DOCX（或图表）后，消息里的文件卡片能下载，但打开预览
报「访问被拒绝：没有预览此文件的权限」。文件名形如
`2a5945067ab84dff8d4b9e4f56a5cd01_乌海全被杀案…docx`。

**根因链**（逐环已核实）：

1. `generate_docx` / `generate_chart` 用**进程级环境变量** `QWENPAW_WORKSPACE_DIR`
   解析输出目录，该变量取不到就回退到全局 `WORKING_DIR/media`（`/app/working/media`）。
2. **整个部署从未设置这个变量**（compose、Dockerfile、脚本里都没有），所以生成的文件
   一律落到全局目录。
3. 而 console 频道的 media 目录是按 **agent 工作区**解析的（`{workspace_dir}/media`），
   工作区来自**请求上下文**，不是环境变量。
4. 预览接口对非管理员执行账号根包含性检查（`files.py` 的
   `path.relative_to(get_user_root(username))`）。普通用户的账号根是
   `/app/working/workspaces/u_<hash>`，与全局 `/app/working/media` 不相交 → 403。
5. 该分支的 detail（`File preview is limited to your account workspace`）在前端
   `preview.error.*` 里**没有对应文案**，因此回退成通用的「没有预览此文件的权限」，
   掩盖了真实原因——这也解释了为什么报错看起来很含糊。

**为什么上传的附件能预览而生成的不能**：上传走 console 频道，落在按 agent 解析的
工作区里；生成走工具内环境变量，落在全局目录。两者路径不一致才是问题所在。

**修复**：两个工具改为复用 `user_files.media_dir_for_current_agent()`（与
`account_tools`、文件解析同一处规则），从请求上下文取 agent 工作区，与 console 频道
保持完全一致；取不到上下文时才回退全局目录（并保留 `CONSOLE_MEDIA_DIR` 覆盖优先）。

**验证**：新增 7 个用例（落点在 agent 工作区内、两个工具一致、环境变量不再能改写落点、
无请求上下文时的回退、`CONSOLE_MEDIA_DIR` 覆盖优先、以及直接模拟预览接口的包含性检查）。
回退验证：恢复旧实现后 5 个用例精确失败。另在容器内按真实账号布局实测，修复后两个工具的
输出目录都位于 `…/u_<hash>/media`（账号根内）。`tests/unit/agents/tools` 321 项回归通过。

**附带的诊断改进建议**（未做）：预览接口的账号越界分支应返回与
`OUTSIDE_WORKSPACE` 同级的可识别错误码，让前端给出「该文件不在你的账号工作区内」
而不是含糊的通用提示——这次排查的额外成本主要就来自这条错误信息没法区分原因。

### 9.18 多用户隔离系统审计：第一阶段（跨账号越权收口）

**背景**：本项目的多用户隔离是在单用户版 QwenPaw 上逐层后加的，此前一直是「遇到一个
问题解决一个」，缺少系统性检查。本次对整个系统做了一轮自上而下的多用户合理性审计，
发现隔离状态**不一致**：有的严格（知识空间、智能体归属、会话、检查点），有的给 admin
旁路（数据分析、文件预览），有的**完全没有校验**。本节记录第一阶段的修复。

**一个决定后续所有判断的前提**：本项目把工具审批硬编码为 `OFF`
（`governance/tool_adapter.py`），且 `security.sandbox_enabled` 默认为 `False` 且部署未
覆盖。这意味着**治理策略引擎与沙箱这两层防护对文件路径完全失效**，唯一还生效的是
「工具自身有没有做包含性检查」。第一阶段先处理与文件路径无关的越权面（下面），
工具层路径限制放在第二阶段。

**本阶段修复的越权面**（均为普通账号可达他人数据）：

| # | 问题 | 影响 | 修复 |
| --- | --- | --- | --- |
| 1 | 备份接口**没有任何身份校验**（`backup.py` 全部 7 个端点无校验函数） | 任何已登录用户可列举/导出/导入/恢复/删除任意备份；备份含所有账号工作区与全局 config | router 级加 `require_admin_request`；菜单 `core.backups` 收归管理员 |
| 2 | cron 的 `@api_action` 自动路由注册在 `/crons/*`（**无 `/api` 前缀**） | 认证中间件对非 `/api` 路径直接放行 → 免认证；且 getter 固定取 `default` 工作区（= admin） | 不再注册这些全局路由；cron 只走带归属校验的 `/api/agents/{id}/cron/*` 与 CLI |
| 3 | agent 编排工具内调本地 API **不带身份**，服务端把「无身份」当作历史的不限权模式（`user_agent_ids(None)` 返回全部 profiles） | `list_agents` 泄露所有账号智能体（含工作区路径）；`chat_with_agent`/`submit_to_agent` 可在他人工作区执行提示词 | 工具内按调用账号过滤智能体；无法解析账号时返回空而非全部 |
| 4 | PawApp 卸载无管理员校验 | 任何用户可删除全局插件目录（`shutil.rmtree`），影响所有账号 | 加 `require_admin_request` |
| 5 | 收件箱 trace 读取无归属校验 | 知道 run_id 即可读他人完整 trace | trace 记录写入 `owner`，读取按账号校验（无 owner 的旧文件按 admin-only 处理） |
| 6 | 推送消息在无 `session_id` 时返回**最近 60 秒所有账号**的消息正文 | 侧边栏/通知轮询是常态路径，会跨账号串消息 | 推送记录写入 `owner`，无 session 时按账号过滤 |

**新增的共用件**：

- `app/routers/_admin_guard.py::require_admin_request`：管理员校验依赖，用在 **router 级**
  而非逐端点——这样以后新增端点不会漏掉校验（本次问题 1 正是「逐端点写、漏了全部」）。
- `user_space.owner_for_agent()` / `owner_for_workspace_dir()`：从 agent id 或工作区路径
  反查归属账号。用于代码只持有 agent id / 工作区路径、无法从请求上下文取账号的场景
  （cron 执行器、心跳、console 频道）。

**验证**：新增 12 个用例（管理员守卫的三态、备份 router 级依赖、推送归属与过滤、trace
归属含旧文件 admin-only、智能体列表过滤与「无身份返回空」）。回退验证：把三处修复分别
退回旧行为后 **6 个用例精确失败**（推送 2 + trace 2 + 智能体列表 2）。更新既有用例
`test_list_agents_data_uses_shared_client`——它原本断言「返回全部智能体」，正是被修复的
行为，现改为显式 mock 归属并补一条无归属不泄露的断言。容器内端到端实测：推送隔离
（alice 只见自己的、bob 只见自己的）、trace 归属（alice 可读、bob 不可读、admin 可读）。
与干净 HEAD 逐项对比失败清单：**未引入新失败**（唯一差异是既有 flaky 用例）。

### 9.19 多用户隔离系统审计：第二阶段（工具层路径限制）

**前提**（决定本阶段必要性）：审批硬编码为 OFF，且 `security.sandbox_enabled` 默认
`False` 且部署未覆盖 —— 治理策略规则与沙箱这两层**都不会执行**。因此唯一还生效的防线
是「工具自身有没有做包含性检查」。

**审计结论**：以下内置工具（均默认启用）**自身零校验**，完全依赖那两层已失效的机制：

| 工具 | 原有问题 |
| --- | --- |
| `read_file` / `write_file` / `edit_file` | 共用解析器对绝对路径原样返回，无包含性检查 |
| `grep_search` / `glob_search` | 搜索根只判「存在」，不判范围 |
| `send_file_to_user` | 可把宿主机上任意可读文件发给用户 |
| `view_image` / `view_video` | 只校验扩展名/MIME |
| `desktop_screenshot` | `path` 参数原样使用 |
| `run_tool_batch` | `file_path` 原样解析 |
| `execute_shell_command` | `cwd` 原样传给 subprocess，命令串无任何限制 |

实测：在**真实治理包装层**（`PolicyGuardedTool` + OFF 模式）下，普通账号可读到他人
工作区文件，甚至读到 `working.secret/auth.json` 的 `jwt_secret`。

**修复**：新增单一规则 `user_files.ensure_inside_account()` —— `resolve()` 后做账号根
包含性校验，越界抛 `AccountScopeError` 并告知「可访问范围 + 本人根目录」。`resolve()`
在前，所以指向账号外的符号链接会被识别而不是跟随。

接入方式按影响面选择，避免逐处重复实现：

- **共享解析器** `file_io._resolve_file_path` 直接接入，一次覆盖 `read`/`write`/`edit`/
  `append`/`grep`/`glob`/`send_file`/`view_media`；四个具体工具改为接住异常并返回可读
  错误（实测若不接住会变成未捕获异常，虽不泄露但不是可用提示）。
- `shell` 的 `cwd` 走同一校验；命令串加「命名了其他账号路径」的拦截。
- `run_tool_batch` 的 `file_path`、`desktop_screenshot` 的 `path` 单独接入。

**能力边界（必须明确）**：shell 的命令串检查是**字符串级尽力而为**，看不出引号、变量、
glob 或运行期才构造的路径（例如 `python -c "open('/…')"`）。它提高误读成本，**不是隔离
边界**。`browser` 工具执行模型写的任意 Python（`browser/execution/worker.py` 注入完整
`__builtins__` 后 `eval`），纯路径校验限不住 —— 要真正隔离需启用沙箱。按既定决策本阶段
**不收回这些工具的权限**，残余风险记录于此。

**验证**：新增 18 用例（符号链接逃逸、`edit`/`append` 未污染目标、四个工具返回可读拒绝
而非异常、shell 越界的 `cwd` 与命令、以及「正常命令与本人路径不受影响」的回归断言）。
回退验证：退回共享解析器与 shell 校验后 **8 个用例精确失败**。容器内端到端实测读他人/
写他人被拒、读自己正常、越界 `cwd` 被拒、`echo && ls` 正常。

**测试基建**：`tests/unit/agents/tools/` 新增 `conftest.py`，让「只测行为」的工具单测在
无账号边界下运行（它们的 fixture 建在 pytest 临时目录，本就在账号根外）；测隔离本身的
用例用 `real_account_scope` 标记跳过该宽化。同时修正 `test_file_io` 中「绝对路径原样返回」
的断言 —— 原样返回正是被修复的行为，现改为断言规范化后仍指向同一文件。

### 9.20 多用户隔离系统审计：第三阶段（共享位置与 admin 语义）

**一、共享存储位置按账号拆分**（原路径不含任何账号成分，仅靠随机 id 分隔）：

| 位置 | 原状 | 现状 |
| --- | --- | --- |
| 数据分析导入暂存 | `<root>/<uuid>/<文件>` | `<root>/<账号>/<uuid>/<文件>`；账号名为非 ASCII（中文用户名）时用 sha256 短哈希，避免不可用路径 |
| 收件箱 trace | `<root>/<run_id>.json` | `<root>/<账号>/<run_id>.json`，读侧新增 `_locate_trace()` 先查账号子树再回落旧的扁平路径，升级期间在跑的 run 不会丢 |
| 长期记忆 `user_id` | 恒为字面量 `"shared"`，`memory_isolation=false` 时所有账号共享一个记忆命名空间 | 始终以归属账号为准；解析不到则退回 `agent_id`（仍不跨账号合并）。`agent_id` 不能替代账号：同一账号多智能体会互相串记忆 |

已确认**本就按账号隔离**、无需改动：`workspaces/<slug>`（账号根）、`users/<slug>/state.json`、
`sessions`、`checkpoints`、`memory`、`chats.json`、`jobs.json`、`.browser-profile`、
`.browser-cdp`，以及知识服务的空间/文档（`owner_id` 严格，admin 也无旁路）。

**二、admin 语义统一**为「与普通用户同等隔离」。此前是**三种状态混用**（知识空间与
智能体归属严格、数据分析与文件预览给 admin 旁路），本身就是隐患 —— 漏改任一处即成为
后门。移除的旁路：数据分析的 `list_tables` owner 过滤 / `get_table` / `delete_table` /
`batch_delete` / `import_api` 三处 / `preview_api` 一处；文件预览的 `is_admin` 短路
（该处叠加 `allow_preview_outside_workspace` 默认 `True`，可让 admin 读宿主机任意非敏感
路径，含他人工作区、`config.json`、备份）；项目目录浏览/设置/导入三处。

**管理能力不受影响**：用户管理、模型/供应商配置、插件安装、备份等本就是 admin-only API，
不属于「看别人文件」。另经确认前端**没有任何**跨用户查看数据表的入口，收紧不会破坏管理
功能。

**三、补漏的管理员校验**（原先任何已登录用户可操作全局资源）：`local-models` 全部端点
（共用一个模型服务与模型目录，改用 router 级守卫）、`settings` 的 `PUT /offload-policy`
（全局设置；同文件的用户级 `/language` 不受影响）、`console` 的
`GET /debug/backend-logs`（全局日志含所有账号活动）。

**验证**：新增 18 用例；回退验证退回暂存、trace、admin 旁路三处后 **4 个用例精确失败**。
容器内端到端确认暂存与 trace 的账号隔离。既有 `inbox/test_inbox_trace_store.py` 原本断言
扁平路径，改为用 store 自身的 `_locate_trace` 定位（让断言关注内容而非布局），并保留一处
直写扁平文件以覆盖「旧布局仍可读」。

### 9.21 多用户隔离审计总结与遗留

**三阶段覆盖范围**：跨账号越权面（备份 / cron 路由 / agent 编排工具 / PawApp 卸载 /
推送 / trace）→ 工具层路径限制（8 个文件类工具 + shell）→ 共享位置按账号拆分 + admin
语义统一。

**尚未处理、需要时单独决策的项**：

1. **沙箱未启用**：`security.sandbox_enabled=False`。启用后可给 `shell`/`browser` 提供
   执行层隔离（镜像内已有 bubblewrap/linux sandbox 实现），但需先在容器内验证其可用性，
   并评估对现有「写 Python 脚本处理数据」这一使用场景的影响。
2. **`browser` 可执行任意 Python**：目前只加了显式路径参数的校验，代码执行能力按既定
   决策保留（见 9.19 的残余风险说明）。
3. **全局单例的可疑项**（未发现跨账号数据泄露，但物理上共享）：语音频道为进程级单例且
   转录用系统临时文件；本地模型服务、`plugins/`、`skill_pool/` 为全局资源（已归管理员
   操作）。
4. **多实例横向扩容**：`TaskTracker` 与实时重放以单主应用实例为边界，需要 Redis/数据库
   协调（见第 10 节未解决清单）。
5. **既有缺陷（非本次引入）**：`data_tables/importer.py:525` 存在 `F821 undefined name
   'Path'`，`agent_context.py` 有两处 `F811` 重复导入 —— 经 stash 对照确认在本次改动前
   就已存在，建议单独修复。

**审计方法论**（供后续复用）：先确认哪些防护层**实际生效**（本项目里治理与沙箱都不生效，
只剩工具自校验），再按「HTTP 全局路由 / agent 作用域路由 / 工具内部」三层分别核对；
每个修复都做**回退验证**（故意移除校验，确认测试精确失败），避免测试只是恰好通过。

### 9.22 三阶段改动的复核（检查是否引入新问题）

对 9.18–9.20 的三阶段改动做了一轮独立复核，方式是与改动前的提交（`04b7ec6a`）
建立 worktree 逐条对比全量 `tests/unit` 的失败清单，而不是只看失败数量。

**结论**：两轮全量运行的失败集合**并集与基线完全一致（104 vs 104，无差集）**，产品代码
未引入功能回归。`cd console && npx tsc -b` 通过。

**发现并修复的问题**：

1. **`owner_for_workspace_dir` 对 admin 默认智能体返回 `None`**（阶段 1 引入的真实缺陷）。
   该函数用 `_account_agent_workspace(owner, agent_id)` 反推路径，而 admin 的默认智能体
   位于 `workspaces/default`，只有 `state.json` 里记录了 `default_agent_id` 才能推出来。
   推送消息的归属依赖它（console 频道拿不到请求身份），解析失败会让消息落成无主，而
   侧边栏/通知轮询恰好是**不带 session_id** 的常态路径，于是 admin 在默认智能体上会静默
   收不到推送。改为**先看 profile 自己声明的 `workspace_dir`**，再回退到规范推导；两个
   账号声明同一目录时返回 `None` 并告警，避免把记录随意归给其中一方。
2. **隔离测试把账号写进了真实工作目录**（阶段 2 引入的测试缺陷）。
   `test_tool_path_confinement.py` 用 `monkeypatch.setenv("QWENPAW_WORKING_DIR", ...)` 想让
   账号根指向临时目录，但 `qwenpaw.constant` 在 import 时就固化了 `WORKING_DIR`，环境变量
   不生效——实测每跑一轮就在 `~/.qwenpaw/workspaces/` 建出 alice/bob 两个假账号。改为直接
   monkeypatch `USER_WORKSPACE_ROOT`/`USER_STATE_ROOT`，并加断言确保账号根落在临时目录内。
3. **符号链接用例不幂等**：同一文件里的 `link.symlink_to(...)` 没有先清理，第二次运行即
   `FileExistsError`（实测 pass → fail）。这正是全量套件曾多出 1 个失败的原因：它会污染
   工作目录，使测试结果取决于是否曾运行过。清理后两轮运行均稳定通过。
4. **`ast_search`/`lsp` 未接住 `AccountScopeError`**：两者共用 `_resolve_file_path`，越界
   会抛异常而非返回结果，与其他文件工具风格不一致（AgentScope 会把异常转成 ERROR 结果，
   不会崩会话，故仅列为不一致）。改为返回可读的拒绝说明。

**确认不是回归的项**（复核时一度怀疑，实测排除）：

- `cron` 的 slash 命令（`cron-list/create/delete`）确实不可用，但**改动前就不可用**：旧
  getter 读 `app_inst.state`，而调用方传入的是 `AppServiceManager`，新旧两种取法都抛
  `AttributeError`。
- 技能里让智能体使用的 `qwenpaw cron ...` 仍然可用：它走 `cli/cron_cmd.py` 的 `/cron/jobs`，
  经 `cli/http.py` 自动补 `/api` 前缀落到 `/api/cron/*`，该路由健在。被移除的只有
  `/crons/*` 那批免认证的自动路由。
- 两个 `deploy/docker-compose.code-mount.*.yml` 解析正常；此前报错只是本地缺 `.env`。
- 全量套件里偶发的 `test_console_chat_reconnect` 失败属**既有 flaky**（单独运行也失败，
  且在两轮基线的失败清单中）。

**复核方法论补充**：对比失败清单时要用**两轮运行的并集**，因为仓库里存在既有 flaky 用例；
且用 worktree 对比时注意 `.gitignore` 全局忽略 `AGENTS.md` 会导致未跟踪的
`src/qwenpaw/agents/md_files/*/AGENTS.md` 缺失，必须先同步这些未跟踪文件，否则会产生
假失败。

### 9.23 图数据库 / 云存储的连接与操作能力核查

**结论**：镜像**具备连接和操作外部图数据库与云存储的能力**，后续单独部署 MinIO /
Neo4j 等集群时可直接使用；但产品当前**没有**这两类功能的一等公民入口（见下）。

**镜像内客户端能力（已实测，非仅 import）**：`scripts/verify_data_service_clients.py`
用本地 mock 跑真实请求，12/12 通过：

| 能力 | 实测结果 |
| --- | --- |
| S3 兼容对象存储（明文 HTTP） | boto3 上传/下载字节一致、列举、删除、生成 SigV4 预签名 URL；MinIO SDK 往返；pandas 经 boto3 读写 parquet |
| S3 兼容对象存储（HTTPS） | TLSv1.3 握手成功；boto3 over HTTPS 往返字节一致（含中文） |
| Neo4j | 驱动发出真实 Bolt 握手（magic `6060b017`）；Cypher 查询构造；`bolt+s` / `neo4j+s` / `neo4j+ssc` 等 TLS 方案均可用 |
| 公共依赖 | 系统 CA 信任库 150 个证书；neo4j 6.3.1、minio 7.2.20、boto3 1.43.98、s3fs 0.4.2、pyarrow 25.0.1 |

**产品当前不具备的**（本轮只核查、未改造）：

1. **没有图数据库工具**。37 个内置工具里无任何图数据库入口，也未连接任何图数据库。
2. **知识服务的图谱不是图数据库**：`entities` / `relations` 等表存在关系型 PG 里，靠
   `space_id` 隔离；LightRAG 用的是默认文件存储，未启用其 Neo4j 图后端。知识服务 24 个
   端点里没有图谱查询接口，UI 只能看到"实体数/关系数"统计。相关工作见 9.19 的检索说明。
3. **云存储完全未接入**：知识服务文件全部落本地文件系统，配置里没有 S3/OSS/COS 的
   endpoint/bucket/凭据项。
4. **未安装的客户端库**：`nebula3`、`gremlin_python`、`rdflib`、`oss2`（阿里云）、
   `azure-storage-blob`、`google-cloud-storage`、`qiniu`、`esdk-obs`（华为）、
   `cos-python-sdk-v5`（腾讯）。其中 S3 兼容协议已由 boto3/MinIO SDK 覆盖，可对接
   MinIO / Ceph 及各家 S3 兼容网关。

**后续扩展的三条路径**（按投入排序）：走 MCP（产品已支持 stdio / streamable_http / sse
三种传输，生态有现成 server，改动最小）→ 做原生工具（纳入现有账号隔离，需开发）→
启用 LightRAG 的 Neo4j 图存储后端（杠杆最大，需先理清与现有 PG 图谱的关系）。

**若将来要接入，必须纳入多用户隔离**：这类是账号级资源，凭据不能全局共享一份（参考
9.18–9.20 的越权教训）。

**注意**：`s3fs==0.4.2` 偏旧（fsspec 2026.9.0 会就此告警），且离线环境无 `aiobotocore`
时 `s3fs` 路径不可用（实测报 `PermissionError: Forbidden`）。要用 s3fs 直读需先修版本
约束；当前已验证可用的替代路径是 boto3 + pandas/pyarrow。

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
| 知识服务部署 | 已完成，4 个组件（api / worker / postgres / lightrag），PG 可独立维护 |
| 图数据库 / 云存储连接能力 | 镜像具备（boto3/MinIO SDK/Neo4j 均已实测连通）；产品尚未提供对应工具入口，见 9.23 |
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
| 聊天文件自动解析 | 已完成，支持 PDF/DOCX/TXT/MD/CSV/XLSX/XLS/JSON |
| DOCX 生成工具 | 已完成，Markdown → DOCX，复用 send_file 投递 |
| 知识文档级联删除 | 已完成，清理实体/关系/LightRAG 图谱 |
| 处理时间追踪 | 已完成，started_at + completed_at + processing_duration |
| 知识/数据分析 UI 统一 | 已完成，排序/筛选/错误详情 |
| 聊天内网化 | 已完成，文件图标/插画/供应商图标全部本地化，alicdn 引用清零 |
| 文件预览增强 | 已完成，docx/xlsx/json 预览 + 解析分块预览 |
| 智能体选择按用户隔离 | 已完成，存储键按用户名加后缀，登录后 hydrate |
| LLM 流式看门狗 | 已完成，首内容+空闲双阶段超时，清理期模型级隔离 |
| think-fold 上下文压缩 | 已完成，请求期折叠已读 ThinkingBlock |
| 工具调用类型修复 | 已完成，schema 引导强转 + 并发签名 bug |
| 数据分析多租户隔离 | 已完成，每用户独立 PG 库 + agent 归属校验 |
| 大文件导入 | 已完成，10GB 流式上传 + 分块导入 |
| 知识服务离线部署 | 已完成，MinerU 模型预置 + auto 降级 + upload_ready 检查 |
| 知识文档去重 | 已完成，SHA-256 秒传 + 列表去重 |
| 知识文档列表查询 | 已修复 LATERAL 漏投影（j.stage），静态回归测试覆盖 |
| 账号用量统计 | 已完成，管理员专属按账号柱状图 + 日期/账号筛选；历史用量标为未归属 |
| 用量可见性 | 已完成，普通用户仅见自己（Web 接口与聊天工具三处出口均已收口） |
| 工具权限快照 | 已修复，新上线工具不再被旧权限名单隐藏；启动时自动自愈存量配置 |
| 生成文件预览 | 已修复，DOCX/图表落点改为按请求的工作区，不再因越界被拒 403 |
| 多用户隔离审计 | 三阶段完成（越权收口 + 工具路径限制 + 共享位置/admin 语义统一），并已复核无新增回归（见 9.22）；沙箱与 browser 代码执行为已知残余 |
| 代码挂载部署体系 | 已完成，合并更新包脚本 + 宿主机日志落盘 |
| 图表生成工具 | 已完成，generate_chart 六种图型，配合 send_file 投递 |
| 流式回答断线恢复 | 已完成心跳、看门狗、短期磁盘重放；重启恢复受保留时间限制 |
| Chat 页面崩溃 | 已增加异常帧容错、分批重放和渲染错误边界；正式浏览器根因仍待日志验证 |
| 知识/数据处理任务恢复 | 已完成启动恢复、每小时重试（最多 3 次）和失败日志片段下载 |

## 11. 维护原则

- 后续功能必须基于 `v2.1.0` 主线继续开发，不要回退到 `1.1.12`。
- 不要修改或覆盖旧版生产数据，升级前先备份 `/app/working`、`/app/working.secret` 和 `/app/working.backups`。
- 不要把 `.env`、API Key、认证文件和用户数据打进代码包。
- ARM64 和 AMD64 镜像、Oracle 客户端、浏览器运行时及 Hadoop/Hive 客户端必须分别验证。
- 代码变更优先采用挂载代码模式测试；只有依赖或底座变化时才重新构建镜像。
- 每次前端改动后都要重新构建 `console/dist` 并重启容器，否则浏览器仍可能加载旧的静态资源。

## 12. 知识服务部署

### 12.1 容器信息

- 容器名称：`huizhi-knowledge-api`
- 端口映射：`18100:18100`
- 数据目录：`/data`
- 代码目录：`/opt/huizhi-knowledge/app/`

### 12.2 模型配置

知识服务使用 OpenAI 兼容接口，模型不打进镜像：

```text
对话模型：gemma-4-26B-A4B-it-MLX-4bit
Embedding：Qwen3-Embedding-0.6B-8bit
Reranker：Qwen3-Reranker-0.6B-4bit
接口地址：http://host.docker.internal:9988/v1
API Key：thinker
```

### 12.3 常用命令

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

### 12.4 API 接口

```text
GET  /health                          健康检查
POST /v1/search                       纯检索（推荐）
POST /v1/query                        RAG 查询（含 answer）
POST /v1/query/stream                 流式 RAG 查询
GET  /v1/documents/{id}/chunks        获取文档分块
GET  /v1/documents/{id}/file          获取原始文件
POST /v1/documents/upload             上传文档
POST /v1/documents/parse              解析文档（返回纯文本）
GET  /v1/spaces                       列出知识空间
GET  /v1/spaces/{id}/documents        列出空间文档
DELETE /v1/documents/{id}             删除文档（级联清理）
```

### 12.5 已知问题

- gemma-4 模型在长上下文中可能被旧内容干扰，建议开启新会话测试
- API key 加密解密失败时需手动替换为明文
- 文档预览需要知识服务返回文件内容，确保文件路径可访问
