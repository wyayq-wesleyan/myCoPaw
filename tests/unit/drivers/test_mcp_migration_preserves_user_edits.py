# -*- coding: utf-8 -*-
"""MCP 迁移写入水位线时，不得覆盖用户刚改过的其它字段。

``_persist_mcp_migration_version`` 在 workspace 启动后不久运行（迁移是异步的）。
早期实现把 ``ws._config`` —— 一份**启动时**加载的快照 —— 整份写回 agent.json，
于是启动窗口内通过 API 做的任何修改都会被静默回滚。

用户可见的形态：新建智能体后立刻改名（或改工具/通道设置），接口返回 200 且带着
新值，但两秒后名字又变回旧的。实测复现率约 50%，取决于迁移完成与用户操作谁先到。

这里锁定两点：只改水位线字段；改名前后写入的其它字段保持不变。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.drivers.adapters import mcp_legacy_config as mcp_mod


@pytest.fixture
def workspace_with_config(tmp_path: Path, monkeypatch):
    """一个 workspace 桩：``_config`` 是过期的快照，磁盘上是较新的内容。

    这里替换 ``load_agent_config`` / ``save_agent_config`` 为「直接读写该文件」的
    实现：被测函数只依赖这两个入口的语义（读当前、写回），替换后仍然能捕获
    「把 ``ws._config`` 整份写回」这种回退 —— 那样磁盘上的 name 会变成旧值。
    """
    import qwenpaw.config.config as cfg_mod
    from qwenpaw.config.config import AgentProfileConfig

    agent_id = "agent-x"
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    path = ws_dir / "agent.json"

    # 磁盘上的「当前」配置：用户已经改过名
    current = {
        "id": agent_id,
        "name": "新名字",
        "description": "用户改过的描述",
        "workspace_dir": str(ws_dir),
        "mcp": {"migration_version": 0},
    }
    path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")

    # workspace 持有的过期快照：仍是启动时的旧值
    stale = AgentProfileConfig.model_validate(
        {
            "id": agent_id,
            "name": "旧名字",
            "description": "",
            "workspace_dir": str(ws_dir),
            "mcp": {"migration_version": 0},
        },
    )

    def fake_load(_aid):
        return AgentProfileConfig.model_validate(
            json.loads(path.read_text(encoding="utf-8")),
        )

    def fake_save(_aid, agent_config):
        payload = agent_config.model_dump(exclude_none=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(cfg_mod, "load_agent_config", fake_load)
    monkeypatch.setattr(cfg_mod, "save_agent_config", fake_save)

    ws = SimpleNamespace(agent_id=agent_id, _config=stale)
    return ws, path


def test_migration_version_write_preserves_other_fields(workspace_with_config) -> None:
    """核心回归：水位线写入后，用户改过的 name/description 必须还在。"""
    ws, path = workspace_with_config

    mcp_mod._persist_mcp_migration_version(ws)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["name"] == "新名字", (
        f"migration reverted the user's rename to {on_disk['name']!r}"
    )
    assert on_disk["description"] == "用户改过的描述"
    assert on_disk["mcp"]["migration_version"] == mcp_mod.CURRENT_MCP_MIGRATION_VERSION


def test_migration_version_is_actually_bumped(workspace_with_config) -> None:
    """修复不能把水位线本身写丢 —— 否则迁移会在每次启动时重跑。"""
    ws, path = workspace_with_config

    mcp_mod._persist_mcp_migration_version(ws)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["mcp"]["migration_version"] == mcp_mod.CURRENT_MCP_MIGRATION_VERSION
    # 进程内的快照也要跟上，避免同一次启动里重复迁移。
    assert ws._config.mcp.migration_version == mcp_mod.CURRENT_MCP_MIGRATION_VERSION


def test_stale_snapshot_never_reaches_disk(workspace_with_config) -> None:
    """直接断言：磁盘上不得出现快照里的旧值。"""
    ws, path = workspace_with_config

    mcp_mod._persist_mcp_migration_version(ws)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["name"] != "旧名字", (
        "the stale workspace snapshot was written to agent.json"
    )
