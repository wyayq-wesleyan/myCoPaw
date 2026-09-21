# -*- coding: utf-8 -*-
"""Tests for runtime prompt contributors."""

from types import SimpleNamespace

import pytest

from qwenpaw.runtime.prompt_contributors import (
    ConversationScopeContributor,
    DataAnalysisContributor,
    FileDeliveryContributor,
    KnowledgeRetrievalContributor,
    WorkspacePromptFilesContributor,
    build_default_prompt_manager,
)


def _ctx(tmp_path, system_prompt_files):
    return SimpleNamespace(
        workspace_dir=str(tmp_path),
        agent_id="test_agent",
        extras={
            "agent_config": SimpleNamespace(
                system_prompt_files=system_prompt_files,
                language="en",
            ),
            "heartbeat_enabled": False,
            "language": "en",
            "memory_manager": None,
        },
    )


def test_workspace_prompt_files_respects_disabled_files(tmp_path):
    """Configured prompt file list gates SOUL.md and PROFILE.md."""
    (tmp_path / "AGENTS.md").write_text("agents body", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("soul body", encoding="utf-8")
    (tmp_path / "PROFILE.md").write_text("profile body", encoding="utf-8")

    prompt = build_default_prompt_manager().build_sync(
        _ctx(tmp_path, ["AGENTS.md"]),
    )

    assert "# AGENTS.md" in prompt
    assert "agents body" in prompt
    assert "# SOUL.md" not in prompt
    assert "soul body" not in prompt
    assert "# PROFILE.md" not in prompt
    assert "profile body" not in prompt


def test_workspace_prompt_files_empty_list_disables_workspace_markdown(
    tmp_path,
):
    """An empty configured list is meaningful and disables markdown files."""
    (tmp_path / "AGENTS.md").write_text("agents body", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("soul body", encoding="utf-8")
    (tmp_path / "PROFILE.md").write_text("profile body", encoding="utf-8")

    fragment = WorkspacePromptFilesContributor().contribute_sync(
        _ctx(tmp_path, []),
    )

    assert fragment is None


def test_workspace_prompt_files_preserves_configured_order_and_custom_files(
    tmp_path,
):
    """Configured file order is the rendered prompt order."""
    (tmp_path / "AGENTS.md").write_text("agents body", encoding="utf-8")
    (tmp_path / "PROFILE.md").write_text("profile body", encoding="utf-8")
    (tmp_path / "CUSTOM.md").write_text("custom body", encoding="utf-8")

    fragment = WorkspacePromptFilesContributor().contribute_sync(
        _ctx(tmp_path, ["PROFILE.md", "CUSTOM.md", "AGENTS.md"]),
    )

    assert fragment is not None
    assert fragment.index("# PROFILE.md") < fragment.index("# CUSTOM.md")
    assert fragment.index("# CUSTOM.md") < fragment.index("# AGENTS.md")


def test_workspace_prompt_files_skips_parent_traversal(tmp_path):
    """Configured prompt files cannot escape the workspace via ``..``."""
    outside = tmp_path.parent / f"{tmp_path.name}_secret.md"
    outside.write_text("secret body", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("agents body", encoding="utf-8")

    fragment = WorkspacePromptFilesContributor().contribute_sync(
        _ctx(tmp_path, [f"../{outside.name}", "AGENTS.md"]),
    )

    assert fragment is not None
    body = str(fragment)
    assert "secret body" not in body
    assert "agents body" in body


def test_workspace_prompt_files_skips_symlink_escape(tmp_path):
    """Configured prompt files cannot escape through symlinks."""
    outside = tmp_path.parent / f"{tmp_path.name}_symlink_secret.md"
    outside.write_text("secret body", encoding="utf-8")
    link = tmp_path / "LINK.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")

    fragment = WorkspacePromptFilesContributor().contribute_sync(
        _ctx(tmp_path, ["LINK.md"]),
    )

    assert fragment is None


def test_conversation_scope_distinguishes_internal_visual_history():
    fragment = ConversationScopeContributor().contribute_sync(
        SimpleNamespace(extras={}),
    )

    assert fragment is not None
    assert "not images uploaded or shown by the user" in fragment
    assert "Previous assistant answers are not evidence" in fragment


def test_file_delivery_requires_chat_attachment_for_user_files():
    fragment = FileDeliveryContributor().contribute_sync(
        SimpleNamespace(extras={"language": "zh"}),
    )

    assert fragment is not None
    assert "send_file_to_user" in fragment
    assert "服务器路径" in fragment


def test_data_analysis_keeps_the_exact_phrase_gate_for_queries():
    """Query tools stay behind「通过数据分析」; the write tools must not loosen it."""
    fragment = DataAnalysisContributor().contribute_sync(SimpleNamespace(extras={}))

    assert fragment is not None
    assert "「通过数据分析」" in fragment
    assert "DO NOT invoke these" in fragment


def test_data_analysis_describes_the_write_tools_and_their_both_conditions():
    fragment = DataAnalysisContributor().contribute_sync(SimpleNamespace(extras={}))

    assert fragment is not None
    assert "create_data_table" in fragment
    assert "import_data_table" in fragment
    # Both conditions, and the explicit "do not create a table" rule.
    assert "BOTH hold" in fragment
    assert "Do NOT create a table" in fragment
    assert "要不要我把它建成数据表" in fragment


def test_knowledge_contributor_covers_saving_an_uploaded_file(monkeypatch):
    monkeypatch.setenv("QWENPAW_KNOWLEDGE_URL", "http://127.0.0.1:18100")

    fragment = KnowledgeRetrievalContributor().contribute_sync(SimpleNamespace(extras={}))

    assert fragment is not None
    assert "create_knowledge_space" in fragment
    assert "upload_knowledge_document" in fragment
    assert "Only upload when the user asks you to keep the file" in fragment


def test_knowledge_contributor_is_absent_without_a_service(monkeypatch):
    monkeypatch.delenv("QWENPAW_KNOWLEDGE_URL", raising=False)

    assert KnowledgeRetrievalContributor().contribute_sync(SimpleNamespace(extras={})) is None


# ---------------------------------------------------------------------------
# Account data boundary
#
# The deployment is multi-user, but the prompt never said so: a model reasoned
# from what the filesystem appeared to contain and would try paths it saw in
# logs or examples, producing confusing "permission" failures. The prompt now
# names the account's own workspace as the boundary.
# ---------------------------------------------------------------------------


def test_account_boundary_names_the_current_workspace():
    from qwenpaw.runtime.prompt_contributors import AccountBoundaryContributor

    ctx = SimpleNamespace(
        workspace_dir="/srv/working/workspaces/u_bob",
        extras={"language": "en"},
    )

    fragment = AccountBoundaryContributor().contribute_sync(ctx)

    assert fragment is not None
    assert "/srv/working/workspaces/u_bob" in fragment
    assert "multi-user" in fragment


def test_account_boundary_is_localized():
    from qwenpaw.runtime.prompt_contributors import AccountBoundaryContributor

    ctx = SimpleNamespace(
        workspace_dir="/srv/working/workspaces/u_bob",
        extras={"language": "zh"},
    )

    fragment = AccountBoundaryContributor().contribute_sync(ctx)

    assert fragment is not None
    assert "账号数据边界" in fragment


def test_account_boundary_is_absent_without_a_workspace():
    from qwenpaw.runtime.prompt_contributors import AccountBoundaryContributor

    ctx = SimpleNamespace(workspace_dir=None, extras={"language": "zh"})

    assert AccountBoundaryContributor().contribute_sync(ctx) is None


def test_each_account_sees_only_its_own_path():
    """The boundary must not be a fixed path shared by every account."""
    import re

    pm = build_default_prompt_manager()
    seen = {}
    for agent_id, workspace in (
        ("user_admin_default", "/srv/working/workspaces/default"),
        ("user_bob_default", "/srv/working/workspaces/u_bob"),
    ):
        ctx = SimpleNamespace(
            workspace_dir=workspace,
            agent_id=agent_id,
            extras={"language": "zh"},
            request=None,
        )
        match = re.search(r"工作区内的文件：`([^`]+)`", pm.build_sync(ctx))
        assert match, f"boundary missing from the prompt for {agent_id}"
        seen[agent_id] = match.group(1)

    assert seen["user_admin_default"] == "/srv/working/workspaces/default"
    assert seen["user_bob_default"] == "/srv/working/workspaces/u_bob"


def test_account_boundary_is_high_priority():
    """It must be assembled before the bulk of the prompt, not appended last."""
    from qwenpaw.runtime.prompt_contributors import AccountBoundaryContributor

    assert AccountBoundaryContributor.priority < 80
