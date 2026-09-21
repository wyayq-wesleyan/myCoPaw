"""Global message search across the caller's own chats and agents.

The user requirement: "I remember a phrase from a while ago, but not which agent
or chat it was in — find it by keyword." That needs to look across every chat the
account owns, which makes confinement the critical property: the scan must never
reach another account's conversations.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _session_file(ws: Path, agent_sub: str, sid: str, uid: str, texts, dates):
    from qwenpaw.app.chats.session import session_filename

    directory = ws / agent_sub / "sessions" / "console"
    directory.mkdir(parents=True, exist_ok=True)
    context = [
        {
            "id": f"m{i}",
            "role": "user" if i % 2 == 0 else "assistant",
            "name": "u",
            "content": [{"type": "text", "text": text}],
            "created_at": date,
        }
        for i, (text, date) in enumerate(zip(texts, dates))
    ]
    path = directory / session_filename(sid, uid)
    path.write_text(
        json.dumps(
            {"agent": {"state": {"context": context}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _chats_file(ws: Path, agent_sub: str, specs):
    directory = ws / agent_sub
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "chats.json").write_text(
        json.dumps(
            {
                "version": 1,
                "chats": [
                    {
                        "id": cid,
                        "name": name,
                        "session_id": sid,
                        "user_id": uid,
                        "channel": "console",
                    }
                    for cid, name, sid, uid in specs
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def search_env(tmp_path, monkeypatch):
    """Two accounts; alice owns two agents, bob owns one."""
    from qwenpaw.config.config import AgentProfileRef, Config

    ws_root = tmp_path / "workspaces"
    _session_file(
        ws_root, "u_alice/agents/botA", "s1", "alice",
        ["去年讨论过光刻机供应链的风险", "好的"],
        ["2026-03-01T10:00:00", "2026-03-01T10:01:00"],
    )
    _chats_file(ws_root, "u_alice/agents/botA", [("c1", "供应链讨论", "s1", "alice")])
    _session_file(
        ws_root, "u_alice/agents/botB", "s2", "alice",
        ["再聊光刻机的替代方案"],
        ["2026-08-15T09:00:00"],
    )
    _chats_file(ws_root, "u_alice/agents/botB", [("c2", "方案B", "s2", "alice")])
    # bob has the same keyword and must never be found by alice.
    _session_file(
        ws_root, "u_bob/agents/botC", "s3", "bob",
        ["bob 也提到光刻机了"],
        ["2026-09-01T00:00:00"],
    )
    _chats_file(ws_root, "u_bob/agents/botC", [("c3", "bob私有", "s3", "bob")])

    config = Config()
    config.agents.profiles = {
        "botA": AgentProfileRef(
            id="botA", workspace_dir=str(ws_root / "u_alice/agents/botA"),
            owner_username="alice",
        ),
        "botB": AgentProfileRef(
            id="botB", workspace_dir=str(ws_root / "u_alice/agents/botB"),
            owner_username="alice",
        ),
        "botC": AgentProfileRef(
            id="botC", workspace_dir=str(ws_root / "u_bob/agents/botC"),
            owner_username="bob",
        ),
    }

    import qwenpaw.config.utils as config_utils
    import qwenpaw.app.user_space as user_space

    monkeypatch.setattr(config_utils, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(user_space, "load_config", lambda *a, **k: config)
    return config


class _Request:
    def __init__(self, username):
        self.state = SimpleNamespace(user=username)
        self.headers = {}


@pytest.mark.asyncio
async def test_search_finds_a_phrase_across_the_callers_agents(search_env):
    from qwenpaw.app.chats.api import search_messages

    response = await search_messages(_Request("alice"), q="光刻机", limit=50)

    agents = {hit.agent_id for hit in response.hits}
    assert agents == {"botA", "botB"}, (
        "a keyword must be found across every agent the account owns"
    )
    # Newest first, so the user sees the most likely conversation first.
    assert response.hits[0].created_at.startswith("2026-08")


@pytest.mark.asyncio
async def test_search_never_reaches_another_account(search_env):
    from qwenpaw.app.chats.api import search_messages

    response = await search_messages(_Request("alice"), q="光刻机", limit=50)

    assert all(hit.agent_id != "botC" for hit in response.hits)
    assert all("bob" not in (hit.snippet or "") for hit in response.hits)


@pytest.mark.asyncio
async def test_search_returns_a_snippet_and_location(search_env):
    """The user needs enough to recognise and open the conversation."""
    from qwenpaw.app.chats.api import search_messages

    response = await search_messages(_Request("alice"), q="供应链", limit=50)

    assert len(response.hits) == 1
    hit = response.hits[0]
    assert "供应链" in hit.snippet
    assert hit.agent_id == "botA"
    assert hit.chat_id == "c1"
    assert hit.chat_name == "供应链讨论"


@pytest.mark.asyncio
async def test_multi_word_query_requires_every_term(search_env):
    """A multi-word query narrows; it is not an OR."""
    from qwenpaw.app.chats.api import search_messages

    both = await search_messages(_Request("alice"), q="光刻机 供应链", limit=50)

    assert len(both.hits) == 1, "only the message containing both terms matches"
    assert "botB" not in {hit.agent_id for hit in both.hits}


@pytest.mark.asyncio
async def test_search_is_case_insensitive(search_env):
    from qwenpaw.app.chats.api import search_messages

    lower = await search_messages(_Request("alice"), q="bob", limit=50)
    upper = await search_messages(_Request("alice"), q="BOB", limit=50)

    assert len(lower.hits) == len(upper.hits)


@pytest.mark.asyncio
async def test_an_account_with_no_agents_searches_nothing(search_env):
    from qwenpaw.app.chats.api import search_messages

    response = await search_messages(_Request("carol"), q="光刻机", limit=50)

    assert response.hits == []
    assert response.scanned_chats == 0
