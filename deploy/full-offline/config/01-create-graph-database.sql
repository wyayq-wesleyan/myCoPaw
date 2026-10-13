-- 知识图谱库（多租户 workspace 存储），见 docs/知识服务多租户存储设计方案.md。
--
-- 单独一个库而不是同库不同 schema：LightRAG 的 13 张表名是固定的，隔离靠
-- workspace 列；独立成库让备份/恢复、连接数核算和误操作隔离都更清晰。
--
-- 仅在数据目录为空（首次初始化）时由 postgres 镜像执行。已存在的库需要手工执行：
--   CREATE DATABASE huizhi_knowledge_graph OWNER huizhi_knowledge;
CREATE DATABASE huizhi_knowledge_graph OWNER huizhi_knowledge;
