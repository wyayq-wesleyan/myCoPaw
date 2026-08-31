# 检索来源元数据测试方案

## 测试目标

验证 `_hybrid_search` 合并后的 evidence 包含正确的 `match_reasons`、`confidence`、`source` 字段，且格式化输出对 LLM 可读。

## 测试矩阵

### 维度一：检索路径覆盖

| 测试场景 | 查询词 | 预期主要来源 | 预期 match_reasons |
|---------|--------|------------|-------------------|
| 全文检索 | "零信任架构" | fulltext | ["fulltext"] |
| 关键词匹配 | "ISO27001认证" | literal | ["literal"] |
| 语义相似 | "网络安全风险评估" | semantic | ["semantic"] |
| 实体匹配 | "张三" | entity + fulltext | ["entity", "fulltext"] |
| 多路命中 | "审计整改" | fulltext + entity | ["fulltext", "entity"] |
| 图谱推理 | "项目管理最佳实践" | lightrag | ["lightrag"] |

### 维度二：置信度归一化

| match_reasons 组合 | 预期 confidence |
|-------------------|----------------|
| fulltext + literal + entity | high |
| fulltext + semantic | high |
| fulltext only | high |
| literal only | high |
| semantic only | medium |
| entity only | medium |
| lightrag only | medium |
| semantic + lightrag | medium |

### 维度三：格式化输出

| 输出目标 | 验证点 |
|---------|--------|
| query_knowledge 工具返回 | 每个文档显示来源标签（全文检索/关键词匹配等） |
| 自动注入上下文 | 每个 snippet 显示来源和置信度 |
| /v1/search 响应 | evidence 每项有 match_reasons, confidence, source |

## 测试文档设计

### 文档1：公司信息安全管理制度.md (~5000字)

**内容概要：**
- 信息安全组织架构：CISO 张三，安全总监 李四
- 零信任架构实施计划（2024年Q3启动）
- SOC2 合规要求，ISO27001 认证周期
- 数据分类分级制度（公开/内部/机密/绝密）
- 安全事件响应流程（15分钟内上报）
- 违规处罚：最高罚款50万元

**可检索特征：**
- 实体：张三、李四、零信任架构、SOC2、ISO27001
- 数字：50万元、15分钟、2024年Q3
- 关键词组合：信息安全管理制度、数据分类分级

### 文档2：2024年年度审计报告.txt (~6000字)

**内容概要：**
- 审计组长：王五，配合部门负责人：张三
- IT一般控制审计发现：12项，已完成整改9项
- 数据完整性审计：发现3处不一致
- 合规性审计结论：基本合规，2项需整改
- 审计建议：加强变更管理流程
- 下次审计时间：2025年6月

**可检索特征：**
- 实体：王五、张三（跨文档出现）
- 数字：12项发现、9项整改、2项需整改
- 关键词：IT一般控制、数据完整性、合规性审计

### 文档3：项目管理规范手册.md (~4000字)

**内容概要：**
- PMO负责人：赵六
- 敏捷开发流程（Scrum + Kanban）
- PRINCE2 方法论要点
- 关键路径法在项目排期中的应用
- 信息安全评审节点（引用文档1）
- 审计合规检查点（引用文档2）
- 项目里程碑：需求评审→设计评审→开发→测试→上线

**可检索特征：**
- 实体：赵六、PRINCE2、关键路径法
- 跨文档关联：信息安全评审（→文档1）、审计合规检查点（→文档2）

## 测试执行流程

### Phase 1：文档灌入

1. 创建三篇测试文档
2. 上传到知识服务
3. 等待 worker 处理完成（轮询 jobs）
4. 验证 chunks、entities、relations 已生成

### Phase 2：检索元数据验证

1. 单路命中测试：每种来源单独验证
2. 多路命中测试：验证 match_reasons 聚合
3. 置信度测试：验证归一化逻辑
4. 空结果测试：无命中时的行为

### Phase 3：工具输出验证

1. query_knowledge 返回格式包含来源标签
2. 自动注入 snippets 包含来源和置信度
3. 前端搜索过滤返回正确文档

### Phase 4：边界情况

1. embedding 不可用时：semantic 路径跳过，confidence 不受影响
2. LightRAG 不可用时：graph_results 为空
3. 混合可用：各路径独立返回，合并正确
4. 大量结果：top_k 截断后来源分布合理

## 预期测试结果

| 测试 | 预期 |
|------|------|
| "零信任架构" 检索 | evidence.source = "fulltext", confidence = "high" |
| "张三" 检索 | evidence 中有两条：一条 entity+fulltext（文档1），一条 entity+fulltext（文档2） |
| "网络安全风险评估" 检索 | evidence.source = "semantic", confidence = "medium" |
| "审计整改" 检索 | evidence.match_reasons 包含 "fulltext" 和 "entity" |
| "项目管理最佳实践" 检索 | evidence 中有 lightrag 来源的条目 |
| 工具返回格式 | 包含 "全文检索"、"语义相似" 等中文标签 |

## 自动化测试脚本

```python
# knowledge-service/tests/test_source_metadata.py

import pytest
import httpx

BASE_URL = "http://localhost:18100"
HEADERS = {
    "X-Knowledge-Key": "huizhi-local-knowledge-dev",
    "X-User-Id": "admin",
    "X-Is-Admin": "true",
}


@pytest.fixture(scope="module")
def space_id():
    resp = httpx.get(f"{BASE_URL}/v1/spaces", headers=HEADERS)
    spaces = resp.json()
    return spaces[0]["id"] if spaces else None


@pytest.fixture(scope="module")
def document_ids(space_id):
    resp = httpx.get(f"{BASE_URL}/v1/documents", headers=HEADERS)
    docs = resp.json()
    return [d["id"] for d in docs]


def search(space_id: str, query: str, top_k: int = 10) -> dict:
    resp = httpx.post(
        f"{BASE_URL}/v1/search",
        headers=HEADERS,
        json={"space_id": space_id, "query": query, "top_k": top_k},
    )
    resp.raise_for_status()
    return resp.json()


class TestSourceMetadata:
    def test_fulltext_source(self, space_id):
        """全文检索命中时 source 应为 fulltext"""
        result = search(space_id, "零信任架构")
        assert len(result["evidence"]) > 0
        e = result["evidence"][0]
        assert e.get("source") == "fulltext"
        assert "fulltext" in e.get("match_reasons", [])
        assert e.get("confidence") in ("high", "medium")

    def test_entity_source(self, space_id):
        """实体匹配时 source 应包含 entity"""
        result = search(space_id, "张三")
        sources = [e.get("source") for e in result["evidence"]]
        match_reasons_all = [e.get("match_reasons", []) for e in result["evidence"]]
        assert any("entity" in reasons for reasons in match_reasons_all)

    def test_multi_source_aggregation(self, space_id):
        """多路命中时 match_reasons 应聚合"""
        result = search(space_id, "审计整改")
        for e in result["evidence"]:
            assert "match_reasons" in e
            assert isinstance(e["match_reasons"], list)

    def test_confidence_normalization(self, space_id):
        """置信度应为 high/medium/low"""
        result = search(space_id, "信息安全")
        for e in result["evidence"]:
            assert e.get("confidence") in ("high", "medium", "low")

    def test_evidence_has_source_field(self, space_id):
        """所有 evidence 都应有 source 字段"""
        result = search(space_id, "管理")
        for e in result["evidence"]:
            assert "source" in e
            assert e["source"] in ("fulltext", "literal", "semantic", "entity", "lightrag", "unknown")

    def test_search_response_structure(self, space_id):
        """响应结构应包含所有必要字段"""
        result = search(space_id, "合规")
        assert "evidence" in result
        assert "documents" in result
        assert "relations" in result
        assert "total_count" in result
        assert "document_count" in result
        assert "semantic_search_enabled" in result
```

## 验证清单

- [ ] 三篇测试文档上传成功
- [ ] chunks 生成完成
- [ ] entities 提取完成
- [ ] relations 提取完成
- [ ] 各检索路径返回正确 source 标签
- [ ] match_reasons 多路聚合正确
- [ ] confidence 归一化逻辑正确
- [ ] 工具返回格式包含来源标签
- [ ] 自动注入包含来源信息
- [ ] 前端搜索过滤正常工作
- [ ] embedding 不可用时不报错
- [ ] 空结果时无异常
