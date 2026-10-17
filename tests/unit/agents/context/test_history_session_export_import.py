# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,protected-access,unused-argument
"""会话导出/导入在 HistoryStore 这一层的往返契约。

导出必须走**原始行**，不能走 ``load_session_messages``：后者重建 ``Msg`` 时会
截断超过 4000 字符的工具结果、并按正文重算 headline。控制台里看不出来，但一个
用来把会话搬到别处的文件不能这样丢东西。

导入必须分配**新的 seq**：``(workspace_key, seq)`` 是主键，沿用源会话的序号会和
源会话本身撞主键。

这组测试锁的就是这两条，以及一个容易写错的地方——结构化列必须**按原始文本**搬，
不能解码后再编码：``tool_input`` 若是裸字符串（存成 ``git status``，没有引号），
``_loads`` 解析不了会返回 ``None``，参数就静默消失了。
"""

from __future__ import annotations

import pytest

from qwenpaw.agents.context.types import LogEntry

from .conftest import requires_platform_db

# 比读取侧 _TRANSCRIPT_TOOL_RESULT_MAX_CHARS (4000) 长，用来证明导出没有被截断。
LONG_TOOL_OUTPUT = "行" * 5000 + "END-OF-TOOL-OUTPUT"


def _source_entries() -> list[tuple[LogEntry, str | None]]:
    return [
        (
            LogEntry(
                kind="model_turn",
                role="user",
                name="user",
                content="帮我统计一下上个月的订单",
                metadata={"timestamp": "2026-10-01T10:00:00+00:00"},
                blocks=[{"type": "text", "text": "帮我统计一下上个月的订单"}],
                created_at="2026-10-01T10:00:00+00:00",
            ),
            "msg-1",
        ),
        (
            LogEntry(
                kind="model_turn",
                role="assistant",
                name="assistant",
                content="我来查一下",
                tool_call_id="call-1",
                # 裸字符串的 tool_input：_to_json 原样存，_loads 读不回来。
                tool_input="git status",
                tool_state="finished",
                headline="⟦ 统计订单 ⟧",
                blocks=[
                    {"type": "text", "text": "我来查一下"},
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "run_shell",
                        "input": {"command": "git status"},
                    },
                ],
                created_at="2026-10-01T10:00:01+00:00",
            ),
            "msg-2",
        ),
        (
            LogEntry(
                kind="tool_result",
                role="assistant",
                name="run_shell",
                content=LONG_TOOL_OUTPUT,
                tool_call_id="call-1",
                tool_state="success",
                # 真实形状：ToolResultBlock 的负载在 ``output`` 里。读取侧只截
                # ``output``；写成 ``text`` 会让截断路径根本走不到，测出来的
                # 「没被截断」是假的。
                blocks=[
                    {
                        "type": "tool_result",
                        "id": "call-1",
                        "name": "run_shell",
                        "output": [{"type": "text", "text": LONG_TOOL_OUTPUT}],
                    },
                ],
                created_at="2026-10-01T10:00:02+00:00",
            ),
            "call-1",
        ),
        (
            LogEntry(
                kind="context_msg",
                role="system",
                name="system",
                content="环境说明",
                # 结构化列：JSON 对象要经 _loads 之后仍然等值。
                metadata={"nested": {"a": [1, 2, 3]}, "flag": True},
                created_at="2026-10-01T10:00:03+00:00",
            ),
            None,
        ),
    ]


def _comparable(row: dict) -> dict:
    """行字段去掉本次导入本就该变的列，其余逐列参与比较。

    ``seq`` 因为重新分配而改变，``session_id`` 因为导入到新会话而改变——这两个
    都不是数据，而是「这份数据现在属于谁」。除此之外任何一列不同都是丢信息。
    """
    return {
        key: value
        for key, value in row.items()
        if key not in ("seq", "session_id")
    }


@requires_platform_db
class TestExportReadsRawRows:
    def test_all_columns_come_back(self, store) -> None:
        store.append_many(session_id="src", entries=_source_entries(), agent_id="a1")

        rows = store.export_session_rows("src", "a1")

        assert len(rows) == 4
        expected = {
            "seq", "session_id", "agent_id", "kind", "role", "name", "content",
            "tool_call_id", "tool_input", "tool_state", "headline", "blocks",
            "metadata", "created_at", "dedup_key",
        }
        assert expected <= set(rows[0]), (
            "导出必须带上全部列；缺列就是导入时静默丢字段："
            f"{sorted(expected - set(rows[0]))}"
        )

    def test_long_tool_result_is_not_truncated(self, store) -> None:
        """读取侧会截断到 4000 字符；导出侧必须原样带出。"""
        store.append_many(session_id="src", entries=_source_entries(), agent_id="a1")

        rows = store.export_session_rows("src", "a1")
        tool_row = next(row for row in rows if row["kind"] == "tool_result")

        assert tool_row["content"] == LONG_TOOL_OUTPUT, (
            "导出的工具结果被改动了：文件必须逐字还原原会话"
        )
        assert len(tool_row["content"]) > 4000

    def test_scoped_to_the_requested_session(self, store) -> None:
        store.append_many(session_id="src", entries=_source_entries(), agent_id="a1")
        store.append_many(
            session_id="other",
            entries=[(LogEntry(kind="context_msg", content="别的会话"), None)],
            agent_id="a1",
        )

        rows = store.export_session_rows("src", "a1")

        assert all(row["session_id"] == "src" for row in rows)
        assert len(rows) == 4

    def test_the_message_path_would_have_truncated_it(self, store) -> None:
        """把「为什么不走 Msg」钉成可验证的事实，而不是注释里的一句话。

        读取侧对工具结果有 4000 字符的展示上限（``_truncate_tool_result``）。导出
        若经 ``load_session_messages`` 中转，长输出就会带着「内容过长已截断」进文件，
        而这个截断在控制台里看不出来——文件看起来正常，内容却已经少了。
        """
        from qwenpaw.agents.context.scroll.history import (
            _TRANSCRIPT_TOOL_RESULT_MAX_CHARS as LIMIT,
        )

        store.append_many(session_id="src", entries=_source_entries(), agent_id="a1")

        exported = store.export_session_rows("src", "a1")
        row_text = next(
            row["content"] for row in exported if row["kind"] == "tool_result"
        )
        assert row_text == LONG_TOOL_OUTPUT, "导出的原始行不应被截断"
        assert len(row_text) > LIMIT, "这个用例要靠超过上限的输出才有意义"

        messages = store.load_session_messages("src", "a1")
        tool_message = next(
            m for m in messages if m.name == "run_shell" and m.content
        )
        out = getattr(tool_message.content[0], "output", None)
        rendered = (
            "".join(getattr(item, "text", "") or "" for item in out)
            if isinstance(out, list)
            else (out or "")
        )
        assert len(rendered) < len(row_text), (
            "读取侧不再截断，这个用例的前提失效了；"
            "确认导出的理由是否还成立"
        )


@requires_platform_db
class TestImportRoundTripsLosslessly:
    def test_export_import_export_is_identical(self, store) -> None:
        """最强的一条：导出 → 导入 → 再导出，两次结果必须逐列一致。

        只允许 ``seq`` 不同（新会话分配新序号）。任何一处解码/重编码丢信息，
        这条都会断在具体的列上。
        """
        store.append_many(session_id="src", entries=_source_entries(), agent_id="a1")
        exported = store.export_session_rows("src", "a1")

        written = store.import_session_rows(
            session_id="dst",
            rows=exported,
            agent_id="a1",
        )
        assert written == len(exported)

        reexported = store.export_session_rows("dst", "a1")

        assert [_comparable(row) for row in reexported] == [
            _comparable(row) for row in exported
        ]

    def test_import_allocates_fresh_sequence_numbers(self, store) -> None:
        """``(workspace_key, seq)`` 是主键，沿用源序号会撞主键。"""
        store.append_many(session_id="src", entries=_source_entries(), agent_id="a1")
        exported = store.export_session_rows("src", "a1")

        store.import_session_rows(session_id="dst", rows=exported, agent_id="a1")

        source_seqs = {row["seq"] for row in exported}
        dest_seqs = {row["seq"] for row in store.export_session_rows("dst", "a1")}
        assert len(dest_seqs) == len(exported), "导入后序号必须互不相同"
        assert source_seqs.isdisjoint(dest_seqs), (
            "导入复用了源会话的 seq：会与源会话行撞主键"
        )

    def test_bare_string_tool_input_survives(self, store) -> None:
        """结构列按原始文本搬运，不做 decode/encode 往返。

        ``tool_input="git status"`` 存进去是不带引号的裸文本。若导入时先
        ``_loads`` 解码，它会解析失败返回 ``None``，工具参数就没了——这条专门
        盯住那个写法。
        """
        store.append_many(session_id="src", entries=_source_entries(), agent_id="a1")
        exported = store.export_session_rows("src", "a1")
        store.import_session_rows(session_id="dst", rows=exported, agent_id="a1")

        rows = store.export_session_rows("dst", "a1")
        call_row = next(row for row in rows if row["kind"] == "model_turn"
                        and row["tool_call_id"] == "call-1")
        assert call_row["tool_input"] == "git status"
        assert call_row["headline"] == "⟦ 统计订单 ⟧"
        assert call_row["tool_state"] == "finished"

    def test_metadata_round_trips_through_the_json_column(self, store) -> None:
        store.append_many(session_id="src", entries=_source_entries(), agent_id="a1")
        exported = store.export_session_rows("src", "a1")
        store.import_session_rows(session_id="dst", rows=exported, agent_id="a1")

        rows = store.export_session_rows("dst", "a1")
        ctx_row = next(row for row in rows if row["kind"] == "context_msg")
        assert '"a": [1, 2, 3]' in (ctx_row["metadata"] or "")
        assert '"flag": true' in (ctx_row["metadata"] or "")

    def test_the_imported_session_reads_back_as_messages(self, store) -> None:
        """导入的历史要能被常规读取路径消费（控制台就是这么展示的）。"""
        store.append_many(session_id="src", entries=_source_entries(), agent_id="a1")
        store.import_session_rows(
            session_id="dst",
            rows=store.export_session_rows("src", "a1"),
            agent_id="a1",
        )

        messages = store.load_session_messages("dst", "a1")

        assert len(messages) == 4
        assert messages[0].get_text_content() == "帮我统计一下上个月的订单"


@requires_platform_db
class TestMalformedRowsAreSkipped:
    def test_rows_without_a_usable_kind_are_dropped(self, store) -> None:
        """文件是用户输入：一行不能用不该让其余会话也导不进来。"""
        store.append_many(session_id="src", entries=_source_entries(), agent_id="a1")
        good = store.export_session_rows("src", "a1")

        rows = [
            {"kind": "not_a_real_kind", "content": "x"},
            {"content": "missing kind"},
            "not even a dict",
            *good,
        ]

        written = store.import_session_rows(session_id="dst", rows=rows, agent_id="a1")

        assert written == len(good)
        assert len(store.export_session_rows("dst", "a1")) == len(good)

    def test_nothing_written_reports_zero(self, store) -> None:
        assert store.import_session_rows(session_id="dst", rows=[], agent_id="a1") == 0
        assert store.import_session_rows(
            session_id="dst", rows=[{"kind": "bogus"}], agent_id="a1",
        ) == 0
