# 数据分析导入改为 COPY 批量入库设计

> **For agentic workers:** 实现请遵循 superpowers:test-driven-development，并配套 docs/superpowers/plans 中的逐任务计划。

**Goal:** 把数据分析的 CSV/XLSX 导入从「`iterrows` 逐行 + `execute_batch` 逐条 INSERT + 每 1000 行提交」改为**单一 COPY 批量入库路径**，让 100 万行级的数据在数秒内落库，并采用「失败即整体失败」的原子语义。

**Architecture:** `src/qwenpaw/data_tables/importer.py` 内的写入段重写为：按列把每个 chunk 向量化序列化成 CSV 文本 → `copy_expert` 流式写入目标表 → 全程单事务，末尾一次 `commit`，异常 `rollback`。建表逻辑保持现状。

**Tech Stack:** Python, pandas, psycopg2 (`copy_expert`), PostgreSQL

---

## 背景与证据

现状（`importer.py`）：

- `_batch_insert`（`importer.py:356`）用 `for _, row in batch.iterrows()`（`:367`）逐行 + 逐格 `_parse_value`（`:369-371`），是纯 Python 单线程的逐行处理。
- 写入用 `psycopg2.extras.execute_batch`（`:374`）：实测其实现是 `[cur.mogrify(sql, args) for args in page]` 后用 `;` 拼成多语句命令——即 100 万行 = 100 万条单行 INSERT 语句，服务端逐条解析。
- `_batch_insert` 每 1000 行 `commit()` 一次（`:364-376`）：100 万行 = 1000 次事务提交（WAL fsync）。

100 万行 / 69MB 的实际耗时由这三者叠加，与本机网络、上传无关（主应用 host 网络、PG 在 127.0.0.1）。

---

## 一、入库机制：单一 COPY 路径（不分流）

**不按行数阈值分流。** 理由：`copy_expert` 固定开销仅毫秒级，不存在性能拐点；且 CSV 是流式分块读取，不读完首块无法得知总行数，阈值判断本身别扭；两套写入实现会带来双倍测试与后续分叉风险（本仓库多次因「同一件事两个实现」出 bug）。

### COPY 语句

```sql
COPY "表名" ("列1","列2",...) FROM STDIN WITH (FORMAT csv)
```

- 每个 chunk（`IMPORT_CHUNK_ROWS` = 10000 行）构造一个 `io.StringIO`，用 `csv.writer` 写行（自动处理逗号/引号/换行/中文），再 `cursor.copy_expert(copy_sql, buf)`。
- **NULL 表示**：`None` 写成不带引号的空字段（CSV 空字段即 NULL），与旧的「空值 → NULL」行为一致。

### 按列类型序列化（替换 `iterrows` + `_parse_value`）

对每个 chunk 的每一列**整列向量化**转换（不做逐格函数调用）：

| 声明类型 | 规则 | 非法值 |
| --- | --- | --- |
| TEXT | 原样输出；空字符串 → NULL | — |
| INTEGER / BIGINT | 解析为数值并要求为整数；输出整数文本 | 抛错 |
| NUMERIC(20,4) | 校验为合法十进制字面量，**原样透传**（避免 float 精度损失） | 抛错 |
| BOOLEAN | 仅接受 `true/false/1/0/是/否/yes/no/y/n`（大小写不敏感），输出 `true`/`false` | 抛错 |
| DATE | 用 `DATE_FORMATS` 整列多格式尝试解析，输出 `YYYY-MM-DD` | 抛错 |
| TIMESTAMP | 用 `TIMESTAMP_FORMATS` 整列多格式尝试解析，输出 `YYYY-MM-DD HH:MM:SS` | 抛错 |

- 空格裁剪等沿用旧行为；保留 `DATE_FORMATS` / `TIMESTAMP_FORMATS`（含中文日期格式）。
- **语义变化（已确认）**：旧 `_parse_value` 对无法转换的值静默写 NULL；新逻辑对「非空且转不动」**抛错**，触发整体失败。空值仍为 NULL。
- 默认列类型是 TEXT（见 `_resolve_column_type` / `test_column_type_defaults.py`），因此绝大多数导入不触发类型转换。

---

## 二、事务与错误语义

- **建表**：保持现状，`_create_real_table` 独立提交（首帧）。
- **数据写入**：`run_import_job` 内，对所有 chunk 的 COPY 共用目标连接、**全程一个事务**；全部完成后 `hg_conn.commit()` 一次。
- **失败**：任意 chunk 的序列化或 COPY 抛错 → `hg_conn.rollback()` → 目标表**零残留**。
  - 目标连接必须 `rollback` 后才归还连接池（`put_target_connection`），避免把半开事务借给下一个用户。
  - 已提交的建表动作（首帧建表）不回滚，失败时可能留下「已建但空」的表；重试可复用其 schema。**此点可接受**（如需失败即删表请在评审时提出）。
- **错误模型**：删除逐行 `insert_errors` 收集与 `partial` 状态。失败只在任务行记一条任务级错误 + 异常栈到 `log_excerpt`；`failed_rows` 语义变为 0 或全部。
- **重试**：外层重试逻辑（`attempts <= 3`、`pending` + `next_retry_at`、耗尽后清理暂存文件）**保持不变**；每次重试都是整单重跑（因为已回滚）。必须保留 `retry = True`、两处 `cleanup_staged_file(file_path)`（有源码级 guard 测试）。

---

## 三、进度可见性

现状：`total_rows` 只在开始置 0（`importer.py:448`），成功行数只在结束时写（`:540-547`），整段导入前端一直看到 `0/0`，观感像卡死。

新增：每写完一个 chunk，用**系统库连接**（`store._get_conn`，与目标库 COPY 事务互不影响）更新该任务的 `success_rows` 与 `total_rows`（均为「已入库行数」），使前端可见进度。结束时写最终 `total_rows` / `success_rows` / `finished_at` / `try` 计数并 `row_count += success`。

---

## 四、顺带清理

- `_remap_frame_columns` 去掉 `df.copy()`：chunk 是 `read_csv` 新产出的独立对象，直接 `df.columns = col_names` 即可。
- 去掉「首帧被 remap 两次」（当前 `:514` 与循环内 `:519`）。
- 删除死代码：`_batch_insert`、`_parse_value`（其职责由整列序列化替代）。
- 保留：并发建表的 `_init_lock_for` 协议、`ensure_path_schema`、路径 schema 逻辑、`cleanup_staged_file`、恢复/重试循环。

---

## 五、文件改动汇总

| 文件 | 改动 |
| --- | --- |
| `src/qwenpaw/data_tables/importer.py` | 新增按列序列化 + COPY 写入助手；重写 `run_import_job` 写入段为单事务 COPY + 进度更新；删除 `_batch_insert` / `_parse_value`；`_remap_frame_columns` 去 copy |
| `tests/unit/data_tables/test_bulk_copy_import.py` | 新增：序列化规则、严格转换、COPY 语句与单事务/回滚行为、整单失败后表内无残留 |

---

## 六、测试

1. **序列化单测**：TEXT 空值→NULL；INT/BIGINT 整数与「1.0」通过、「1.5」/「abc」抛错；NUMERIC 原样透传；BOOLEAN 合法集合与非法值；DATE/TIMESTAMP 多格式（含中文）与非法值。
2. **COPY 语句**：断言生成的 SQL 为 `COPY "表" ("列"...) FROM STDIN WITH (FORMAT csv)`，CSV 内容含正确的引号/NULL。
3. **单事务与整体失败**（fake 连接）：模拟第 N 个 chunk COPY 抛错 → 断言调用了 `rollback`、**未** `commit`、任务被记为失败（不 partial）。
4. **成功路径**（fake 连接）：断言末尾 `commit` 一次、`success_rows == total_rows == 总行数`、调用了 `cleanup_staged_file`。
5. **回归**：现有 `test_column_type_defaults.py`、`test_concurrent_initialization.py`、`test_staged_file_cleanup.py`、`test_target_connection_pool_key.py` 必须仍通过。

---

## 七、验证方式

1. 单测全绿（`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/data_tables -q`）。
2. 用一份 ~100 万行 / ~69MB 的 CSV 在部署环境导入，观察：
   - 任务状态在 `loading` 期间 `success_rows` 持续增长（不再 0/0）；
   - 总耗时较改造前显著下降（目标从「分钟级」降到「数秒级」，以实测为准）；
   - 导入完成后 `SELECT count(*)` 与文件行数一致。
3. 构造一个含坏值（类型列写入非数字）的文件 → 任务 `failed`，目标表**无该批次任何行**。

---

## 八、风险与取舍

- **类型列严格化**：以前静默 NULL 的坏值现在会整单失败。默认 TEXT 不受影响；仅显式 AUTO / 固定类型的列受影响。已确认接受。
- **单事务体积**：千万行级单事务会持有较多 WAL / 锁，但目标表在导入期间不对外提供查询（按 job 串行），可接受；如后续需要可按大块分段提交（会牺牲「整体失败」原子性，本次不做）。
