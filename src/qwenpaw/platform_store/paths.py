# -*- coding: utf-8 -*-
"""Path scoping: isolating resources *inside* one account.

A user's knowledge and data resources were one flat bucket per account. Case
work needs a second isolation dimension: separate cases must not see each other's
material, while shared background (investigation techniques, methodology) stays
visible to several of them. A **path** is that container — a node in a tree that
both a knowledge space and a data table can belong to, and that an agent is
granted access to.

Design rules that the rest of the code depends on:

* **A resource belongs to exactly one path.** Enforced by the primary key on
  ``resource_bindings``. If a document could sit in two paths, editing it for
  one case would silently change the other — the opposite of the isolation this
  exists to provide. Shared material is shared by putting it in a common *parent*
  path that both agents are granted, not by duplicating the binding.

* **A parent path does not implicitly include its children.** Resources belong to
  the path they are bound to. Without this rule a root grant would expose every
  case underneath it, turning the tree back into the flat bucket it replaces.

* **Fail-closed.** An agent with no bindings sees nothing. The upgrade grants
  every existing agent its account's existing paths, so behaviour is unchanged
  on the day of the upgrade — but anything created afterwards is invisible until
  explicitly granted.

Storage is the ``platform`` schema (the platform's own data) and every statement
is scoped by ``owner_username``, so one account can never reach another's tree.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

import psycopg2

from .migrations import PATH_SCOPE_MIGRATIONS
from .pool import dict_cursor, get_conn, put_conn
from .schema import ensure_schema

logger = logging.getLogger(__name__)

# ``resource_kind`` values. Kept as an explicit closed set so a typo cannot
# silently create an unreachable binding.
KIND_KNOWLEDGE_SPACE = "knowledge_space"
KIND_DATA_TABLE = "data_table"
RESOURCE_KINDS = (KIND_KNOWLEDGE_SPACE, KIND_DATA_TABLE)

# Schema-name prefix for a path's data tables (see :func:`data_schema_for`).
DATA_SCHEMA_PREFIX = "p_"

class PathScopeError(ValueError):
    """A path operation that the caller must fix (readable message)."""


@dataclass
class PathNode:
    """One node of an account's path tree."""

    id: str
    owner_username: str
    name: str
    parent_id: str | None
    is_root: bool
    children: list["PathNode"] = field(default_factory=list)

    def walk(self) -> Iterable["PathNode"]:
        """Depth-first over this node and all descendants."""
        yield self
        for child in self.children:
            yield from child.walk()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "parent_id": self.parent_id,
            "is_root": self.is_root,
            "children": [child.as_dict() for child in self.children],
        }


def data_schema_for(path_id: str) -> str:
    """The PostgreSQL schema holding one path's data tables.

    Derived deterministically from the path id rather than stored, so it cannot
    drift out of sync. Only ``[a-z0-9_]`` characters are produced, which matters
    because a schema name cannot be a bind parameter — it is interpolated into
    DDL and ``SET search_path``, so it must never be able to carry a quote or a
    separator.

    **The whole hex form is used, not a prefix.** A UUID's 32 hex characters carry
    entropy in all four of its segments, so truncating (say to the first 24)
    discards the last segment and lets two distinct paths collide onto one schema
    — which would silently merge two cases' data tables. ``p_`` plus 32 chars is
    34, comfortably inside PostgreSQL's 63-character identifier limit.
    """
    compact = re.sub(r"[^0-9a-f]", "", str(path_id).lower())
    if len(compact) != 32:
        raise PathScopeError(f"路径 id 不是有效的 UUID：{path_id!r}")
    return f"{DATA_SCHEMA_PREFIX}{compact}"


def _ensure_ready() -> None:
    """Apply the path-scope schema once per process."""
    ensure_schema(PATH_SCOPE_MIGRATIONS)


def _query(sql: str, params: tuple | list = ()) -> list[dict]:
    conn = get_conn()
    try:
        with dict_cursor(conn) as cur:
            cur.execute(sql, params or ())
            rows = [dict(row) for row in cur.fetchall()]
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def _execute(sql: str, params: tuple | list = ()) -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            count = cur.rowcount
        conn.commit()
        return int(count)
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# --- tree ------------------------------------------------------------------


def ensure_root_path(owner_username: str, name: str = "默认") -> str:
    """Get (or create) the account's single root path; returns its id.

    Every account has exactly one root — enforced by a partial unique index.
    This is where an account's pre-existing flat resources migrate to, so it is
    also the anchor the upgrade uses.
    """
    _ensure_ready()
    owner = str(owner_username or "").strip()
    if not owner:
        raise PathScopeError("路径必须属于一个账号。")

    existing = _query(
        'SELECT id FROM "platform".paths '
        "WHERE owner_username = %s AND is_root LIMIT 1",
        (owner,),
    )
    if existing:
        return existing[0]["id"]

    path_id = str(uuid.uuid4())
    try:
        _execute(
            'INSERT INTO "platform".paths '
            "(id, owner_username, name, parent_id, is_root) "
            "VALUES (%s, %s, %s, NULL, true)",
            (path_id, owner, name or "默认"),
        )
    except psycopg2.errors.UniqueViolation:
        # A concurrent startup created it first; take theirs.
        found = _query(
            'SELECT id FROM "platform".paths '
            "WHERE owner_username = %s AND is_root LIMIT 1",
            (owner,),
        )
        if not found:
            raise
        return found[0]["id"]
    return path_id


def create_path(
    owner_username: str,
    name: str,
    *,
    parent_id: str | None = None,
) -> str:
    """Create a child path. ``parent_id=None`` targets the root."""
    _ensure_ready()
    owner = str(owner_username or "").strip()
    if not owner:
        raise PathScopeError("路径必须属于一个账号。")
    clean_name = str(name or "").strip()
    if not clean_name:
        raise PathScopeError("路径名称不能为空。")
    if len(clean_name) > 120:
        raise PathScopeError("路径名称过长（超过 120 字符）。")

    if parent_id is None:
        parent_id = ensure_root_path(owner)
    else:
        parent_id = _require_owned_path(parent_id, owner)

    path_id = str(uuid.uuid4())
    try:
        _execute(
            'INSERT INTO "platform".paths '
            "(id, owner_username, name, parent_id, is_root) "
            "VALUES (%s, %s, %s, %s, false)",
            (path_id, owner, clean_name, parent_id),
        )
    except psycopg2.errors.UniqueViolation as exc:
        raise PathScopeError(
            f"同一层下已存在名为「{clean_name}」的路径。",
        ) from exc
    return path_id


def _require_owned_path(path_id: str, owner_username: str) -> str:
    """Ensure *path_id* exists and belongs to *owner_username*."""
    rows = _query(
        'SELECT id FROM "platform".paths '
        "WHERE id = %s AND owner_username = %s",
        (path_id, owner_username),
    )
    if not rows:
        raise PathScopeError("路径不存在，或不属于当前账号。")
    return rows[0]["id"]


def list_path_tree(owner_username: str) -> list[PathNode]:
    """The account's whole tree, roots first (there is exactly one root)."""
    _ensure_ready()
    rows = _query(
        'SELECT id, owner_username, name, parent_id, is_root '
        'FROM "platform".paths WHERE owner_username = %s ORDER BY is_root DESC, name',
        (owner_username,),
    )
    nodes = {
        row["id"]: PathNode(
            id=row["id"],
            owner_username=row["owner_username"],
            name=row["name"],
            parent_id=row["parent_id"],
            is_root=bool(row["is_root"]),
        )
        for row in rows
    }
    roots: list[PathNode] = []
    for node in nodes.values():
        parent = nodes.get(node.parent_id) if node.parent_id else None
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)
    return roots


def path_by_id(path_id: str) -> dict[str, Any] | None:
    """Raw path row (no tree), or ``None``."""
    _ensure_ready()
    rows = _query(
        'SELECT id, owner_username, name, parent_id, is_root '
        'FROM "platform".paths WHERE id = %s',
        (path_id,),
    )
    return rows[0] if rows else None


def rename_path(path_id: str, name: str) -> None:
    clean = str(name or "").strip()
    if not clean:
        raise PathScopeError("路径名称不能为空。")
    try:
        _execute(
            'UPDATE "platform".paths SET name = %s WHERE id = %s',
            (clean, path_id),
        )
    except psycopg2.errors.UniqueViolation as exc:
        raise PathScopeError(f"同一层下已存在名为「{clean}」的路径。") from exc


def move_path(path_id: str, new_parent_id: str | None) -> None:
    """Reparent a path, refusing moves that would create a cycle.

    Dragging a node into its own subtree would detach that subtree from the tree
    entirely (it would reference itself as an ancestor), so the check is not
    optional — the UI cannot be trusted to prevent it, and a manual API call
    certainly cannot.
    """
    _ensure_ready()
    node = path_by_id(path_id)
    if node is None:
        raise PathScopeError("路径不存在。")
    if node["is_root"]:
        raise PathScopeError("根路径不能移动。")

    owner = node["owner_username"]
    if new_parent_id is None:
        target_parent = ensure_root_path(owner)
    else:
        target_parent = _require_owned_path(new_parent_id, owner)

    if target_parent == path_id:
        raise PathScopeError("不能把路径移动到它自己下面。")

    # Walk UP from the target: if this node is among the target's ancestors,
    # the move would place the node inside its own subtree.
    ancestors_of_target: set[str] = set()
    cursor: str | None = target_parent
    while cursor is not None:
        ancestors_of_target.add(cursor)
        row = path_by_id(cursor)
        cursor = row["parent_id"] if row else None
    if path_id in ancestors_of_target:
        raise PathScopeError("不能把路径移动到它自己的子树下面。")

    try:
        _execute(
            'UPDATE "platform".paths SET parent_id = %s WHERE id = %s',
            (target_parent, path_id),
        )
    except psycopg2.errors.UniqueViolation as exc:
        raise PathScopeError("目标位置已有同名路径。") from exc


def path_is_empty(path_id: str) -> bool:
    """Whether a path holds no resources (children are not resources)."""
    rows = _query(
        'SELECT 1 FROM "platform".resource_bindings WHERE path_id = %s LIMIT 1',
        (path_id,),
    )
    if rows:
        return False
    children = _query(
        'SELECT 1 FROM "platform".paths WHERE parent_id = %s LIMIT 1',
        (path_id,),
    )
    return not children


def delete_path(path_id: str) -> None:
    """Delete a path. Only empty paths (no resources, no children) may go.

    Deleting a path with material in it would drop the bindings (``CASCADE``)
    and strand the documents/tables, and for a case folder that means losing an
    entire case to one click. Requiring the caller to move things out first makes
    the destructive step explicit.
    """
    _ensure_ready()
    node = path_by_id(path_id)
    if node is None:
        raise PathScopeError("路径不存在。")
    if node["is_root"]:
        raise PathScopeError("根路径不能删除。")
    if not path_is_empty(path_id):
        raise PathScopeError(
            "路径下还有内容（文档、数据表或子路径），请先移走再删除。",
        )
    _execute('DELETE FROM "platform".paths WHERE id = %s', (path_id,))


# --- resource bindings -----------------------------------------------------


def bind_resource(path_id: str, resource_kind: str, resource_id: str) -> None:
    """Bind a resource to a path, taking it away from any previous path.

    Moving (rather than failing on conflict) is what makes "把公共路径下的文档
    移到新路径" a single call, and it keeps the one-resource-one-path invariant
    intact.
    """
    _ensure_ready()
    if resource_kind not in RESOURCE_KINDS:
        raise PathScopeError(f"未知的资源类型 {resource_kind!r}")
    node = path_by_id(path_id)
    if node is None:
        raise PathScopeError("路径不存在。")
    _execute(
        'INSERT INTO "platform".resource_bindings '
        "(path_id, resource_kind, resource_id) VALUES (%s, %s, %s) "
        "ON CONFLICT (resource_kind, resource_id) "
        "DO UPDATE SET path_id = EXCLUDED.path_id, created_at = now()",
        (path_id, resource_kind, str(resource_id)),
    )


def unbind_resource(resource_kind: str, resource_id: str) -> None:
    _execute(
        'DELETE FROM "platform".resource_bindings '
        "WHERE resource_kind = %s AND resource_id = %s",
        (resource_kind, str(resource_id)),
    )


def path_of_resource(resource_kind: str, resource_id: str) -> str | None:
    rows = _query(
        'SELECT path_id FROM "platform".resource_bindings '
        "WHERE resource_kind = %s AND resource_id = %s",
        (resource_kind, str(resource_id)),
    )
    return rows[0]["path_id"] if rows else None


def resources_in_path(path_id: str) -> list[dict[str, Any]]:
    return _query(
        'SELECT resource_kind, resource_id FROM "platform".resource_bindings '
        "WHERE path_id = %s ORDER BY resource_kind, resource_id",
        (path_id,),
    )


# --- agent bindings --------------------------------------------------------


def bind_agent(agent_id: str, path_id: str) -> None:
    _ensure_ready()
    agent = str(agent_id or "").strip()
    if not agent:
        raise PathScopeError("必须指定智能体。")
    if path_by_id(path_id) is None:
        raise PathScopeError("路径不存在。")
    _execute(
        'INSERT INTO "platform".agent_path_bindings (agent_id, path_id) '
        "VALUES (%s, %s) ON CONFLICT (agent_id, path_id) DO NOTHING",
        (agent, path_id),
    )


def unbind_agent(agent_id: str, path_id: str) -> None:
    _execute(
        'DELETE FROM "platform".agent_path_bindings '
        "WHERE agent_id = %s AND path_id = %s",
        (str(agent_id), path_id),
    )


def agent_path_ids(agent_id: str) -> list[str]:
    """Path ids granted to an agent. Empty list means nothing is visible."""
    _ensure_ready()
    rows = _query(
        'SELECT path_id FROM "platform".agent_path_bindings WHERE agent_id = %s',
        (str(agent_id or ""),),
    )
    return [row["path_id"] for row in rows]


def agents_for_path(path_id: str) -> list[str]:
    rows = _query(
        'SELECT agent_id FROM "platform".agent_path_bindings '
        "WHERE path_id = %s ORDER BY agent_id",
        (path_id,),
    )
    return [row["agent_id"] for row in rows]


def effective_path_of(
    resource_kind: str,
    resource_id: str,
    owner_username: str,
) -> str:
    """The path a resource is reachable through: its binding, else the root.

    **Why unbound resources fall back to the root rather than being invisible.**
    Every resource that existed before paths were introduced has no binding, and
    enumerating them all at upgrade time (knowledge spaces live behind the
    knowledge service, data tables live in per-account databases) would be
    fragile: anything missed would silently disappear from every agent's view.
    Treating "unbound" as "in the root path" makes the root the account's public
    area — visible to every agent without an explicit grant — and makes the
    upgrade a no-op for existing content.

    The consequence to keep in mind: to isolate material you must *bind it to a
    child path*; leaving it unbound keeps it public.
    """
    bound = path_of_resource(resource_kind, resource_id)
    if bound is not None:
        return bound
    return ensure_root_path(owner_username)


def is_visible_to_agent(
    *,
    agent_id: str,
    resource_kind: str,
    resource_id: str,
    owner_username: str,
) -> bool:
    """Whether an agent may reach one resource.

    The single predicate every enforcement point should use. It resolves the
    resource's effective path and checks it against the agent's grants, so a
    caller cannot drift by reimplementing the rule (for example by forgetting the
    root fallback and hiding content that should be public).
    """
    allowed = set(agent_path_ids(agent_id))
    if not allowed:
        # fail-closed: an agent with no grants sees nothing at all.
        return False
    return (
        effective_path_of(resource_kind, resource_id, owner_username) in allowed
    )


def allowed_resource_ids(agent_id: str, resource_kind: str) -> list[str]:
    """Resource ids of *resource_kind* an agent may reach, across its paths.

    This is the single lookup the enforcement points use. It resolves
    agent → paths → resources, so a caller cannot accidentally search a path the
    agent was not granted, and a resource moved between paths follows its new
    path automatically (no second place to update).
    """
    _ensure_ready()
    paths = agent_path_ids(agent_id)
    if not paths:
        return []
    rows = _query(
        'SELECT resource_id FROM "platform".resource_bindings '
        "WHERE resource_kind = %s AND path_id = ANY(%s) "
        "ORDER BY resource_id",
        (resource_kind, paths),
    )
    return [row["resource_id"] for row in rows]


def grant_root_to_agent(owner_username: str, agent_id: str) -> str:
    """Grant an agent the account's root path (its public area); returns path id.

    This is the one grant the upgrade performs on every agent: the root is the
    account-wide bucket every agent could already read, so granting it keeps
    visibility unchanged. Child paths are intentionally left ungranted — that is
    what makes a newly created case invisible until it is attached explicitly.
    """
    root = ensure_root_path(owner_username)
    bind_agent(agent_id, root)
    return root


def grant_all_existing_paths(owner_username: str, agent_id: str) -> int:
    """Grant an agent every path its account currently has. Returns the count.

    Used by the upgrade so that agents keep exactly the visibility they had
    before paths existed. Deliberately only called from migration: a new path
    must not become visible to existing agents by itself, or the isolation this
    feature provides would never actually apply.
    """
    _ensure_ready()
    paths = _query(
        'SELECT id FROM "platform".paths WHERE owner_username = %s',
        (owner_username,),
    )
    granted = 0
    for row in paths:
        _execute(
            'INSERT INTO "platform".agent_path_bindings (agent_id, path_id) '
            "VALUES (%s, %s) ON CONFLICT (agent_id, path_id) DO NOTHING",
            (str(agent_id), row["id"]),
        )
        granted += 1
    return granted
