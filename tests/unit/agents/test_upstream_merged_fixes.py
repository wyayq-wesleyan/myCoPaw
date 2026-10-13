# -*- coding: utf-8 -*-
"""Regressions for the upstream fixes merged into this branch.

Each test pins a failure that was real — either reproduced here or reported from
the deployment — so a later refactor cannot quietly undo it.

| Fix | Failure it prevents |
| --- | --- |
| empty text blocks | a reasoning-only turn persists ``TextBlock(text="")``; providers reject it on every later replay, poisoning the session |
| PDF document blocks | ``file`` parts are rejected by OpenAI-compatible servers (vLLM/DeepSeek/DashScope) with 400 |
| stream cleanup quarantine | a cleanup task that never finishes disables the provider until restart |
| image pixel limit | an oversized base64 image inflates the request and the stored context |
| document pagination | past 100 documents, legitimate hits look unauthorized to the retrieval filter |
"""

from __future__ import annotations

import base64
import io
import os

import pytest
from PIL import Image

from qwenpaw.agents.utils.message_request_normalizer import (
    _is_document_block,
    _strip_media_blocks_in_place,
)
from qwenpaw.agents.utils.text_block_utils import (
    drop_empty_text_blocks,
    is_empty_text_block,
    sanitize_empty_text_blocks,
)
from qwenpaw.utils.image_resize import (
    MAX_IMAGE_PIXELS_ENV,
    _resized_dimensions,
    get_max_image_pixels,
    resize_base64_image,
)


# ---------------------------------------------------------------------------
# Empty text blocks
# ---------------------------------------------------------------------------


def test_empty_text_block_is_recognised_in_both_shapes():
    """State may hold dict blocks (1.x) or Pydantic blocks (2.0)."""
    from types import SimpleNamespace

    assert is_empty_text_block({"type": "text", "text": ""})
    assert is_empty_text_block(SimpleNamespace(type="text", text=""))


def test_whitespace_only_block_is_kept():
    """Providers reject the empty string, not a newline.

    Keeping whitespace avoids changing what the model sees in its own history.
    """
    assert not is_empty_text_block({"type": "text", "text": "\n"})
    assert not is_empty_text_block({"type": "thinking", "text": ""})


def test_drop_only_removes_empty_text_blocks():
    blocks = [
        {"type": "text", "text": ""},
        {"type": "text", "text": "answer"},
        {"type": "thinking", "text": ""},
    ]

    kept = drop_empty_text_blocks(blocks)

    assert [b["type"] for b in kept] == ["text", "thinking"]
    assert kept[0]["text"] == "answer"


def test_sanitize_heals_an_already_poisoned_session():
    """Sessions written before this guard existed must be repaired on load."""
    from types import SimpleNamespace

    msg = SimpleNamespace(
        content=[{"type": "text", "text": ""}, {"type": "text", "text": "ok"}],
    )

    sanitize_empty_text_blocks([msg])

    assert msg.content == [{"type": "text", "text": "ok"}]


def test_sanitize_leaves_healthy_messages_untouched():
    from types import SimpleNamespace

    content = [{"type": "text", "text": "hello"}]
    msg = SimpleNamespace(content=list(content))

    sanitize_empty_text_blocks([msg])

    assert msg.content == content


# ---------------------------------------------------------------------------
# PDF document blocks
# ---------------------------------------------------------------------------


def test_pdf_blocks_are_detected_but_images_are_not():
    assert _is_document_block({"type": "file"})
    assert _is_document_block(
        {"type": "data", "source": {"media_type": "application/pdf"}},
    )
    assert not _is_document_block(
        {"type": "data", "source": {"media_type": "image/png"}},
    )


def test_tool_result_pdf_is_stripped_but_user_pdf_is_kept():
    """Only tool results are stripped, and only documents.

    A user-supplied document keeps the formatter's own path, and images stay —
    those parts are accepted by OpenAI-compatible endpoints.
    """
    from types import SimpleNamespace

    msg = SimpleNamespace(
        content=[
            {"type": "data", "source": {"media_type": "application/pdf"}},
            {
                "type": "tool_result",
                "output": [
                    {"type": "data", "source": {"media_type": "application/pdf"}},
                    {"type": "data", "source": {"media_type": "image/png"}},
                ],
            },
        ],
    )

    stripped = _strip_media_blocks_in_place(
        [msg],
        document_only=True,
        tool_result_only=True,
    )

    assert stripped == 1
    assert msg.content[0]["source"]["media_type"] == "application/pdf"
    remaining = [b["source"]["media_type"] for b in msg.content[1]["output"]]
    assert remaining == ["image/png"]


def test_document_only_does_not_strip_images_at_top_level():
    from types import SimpleNamespace

    msg = SimpleNamespace(
        content=[{"type": "data", "source": {"media_type": "image/png"}}],
    )

    assert (
        _strip_media_blocks_in_place(
            [msg],
            document_only=True,
            tool_result_only=True,
        )
        == 0
    )


# ---------------------------------------------------------------------------
# Image pixel limit
# ---------------------------------------------------------------------------


@pytest.fixture
def jpeg_b64():
    image = Image.new("RGB", (400, 400), (10, 120, 200))
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_resize_shrinks_an_oversized_image(jpeg_b64):
    out, changed = resize_base64_image(jpeg_b64, 10_000)

    assert changed
    resized = Image.open(io.BytesIO(base64.b64decode(out)))
    assert resized.size[0] * resized.size[1] <= 10_000
    assert len(out) < len(jpeg_b64)


def test_image_within_the_limit_is_left_byte_identical(jpeg_b64):
    """No re-encode when unnecessary: recompressing would lose quality."""
    out, changed = resize_base64_image(jpeg_b64, 10_000_000)

    assert not changed
    assert out == jpeg_b64


def test_limit_of_zero_disables_resizing(jpeg_b64):
    out, changed = resize_base64_image(jpeg_b64, 0)

    assert not changed
    assert out == jpeg_b64


def test_dimensions_never_exceed_the_limit():
    """Flooring then trimming: rounding up can land one pixel over."""
    for width, height in ((1000, 1000), (1920, 1080), (333, 777)):
        rw, rh = _resized_dimensions(width, height, 4096)
        assert rw * rh <= 4096


def test_resize_failure_raises_rather_than_passing_original_through():
    """Silently sending the original would defeat the limit entirely."""
    with pytest.raises(ValueError):
        resize_base64_image("not-base64", 1000)


def test_config_reads_and_validates_the_environment(monkeypatch):
    monkeypatch.delenv(MAX_IMAGE_PIXELS_ENV, raising=False)
    assert get_max_image_pixels() == 0

    monkeypatch.setenv(MAX_IMAGE_PIXELS_ENV, "1048576")
    assert get_max_image_pixels() == 1_048_576

    monkeypatch.setenv(MAX_IMAGE_PIXELS_ENV, "-5")
    with pytest.raises(ValueError):
        get_max_image_pixels()

    monkeypatch.setenv(MAX_IMAGE_PIXELS_ENV, "abc")
    with pytest.raises(ValueError):
        get_max_image_pixels()


# ---------------------------------------------------------------------------
# Stream cleanup quarantine
# ---------------------------------------------------------------------------


def test_quarantine_has_a_deadline_and_expires(monkeypatch):
    """A wedged cleanup task must not disable the provider until restart."""
    from qwenpaw.providers import retry_chat_model as m

    assert m._STREAM_CLEANUP_QUARANTINE_TIMEOUT > 0

    key = "test-provider:test-model"
    model = object.__new__(m.RetryChatModel)

    class _Inner:
        _provider_id = "test-provider"
        model = "test-model"

    model._inner = _Inner()

    # A live quarantine rejects...
    m._pending_provider_cleanup_tasks_by_model[key] = {object()}
    m._pending_provider_cleanup_deadlines_by_model.pop(key, None)
    with pytest.raises(m.StreamCleanupPendingError):
        model._ensure_provider_available()

    # ...but one whose deadline has passed releases and cleans up.
    m._pending_provider_cleanup_deadlines_by_model[key] = 0.0
    model._ensure_provider_available()
    assert key not in m._pending_provider_cleanup_tasks_by_model
    assert key not in m._pending_provider_cleanup_deadlines_by_model


# ---------------------------------------------------------------------------
# Document pagination (path isolation)
# ---------------------------------------------------------------------------


def test_tool_side_document_fetch_paginates():
    """The allowed-set must cover every page.

    A truncated list makes documents past page 1 look unauthorized, so a
    granted document is dropped and the agent reports "no relevant material".
    """
    import inspect

    from qwenpaw.agents.tools import knowledge as tool_mod

    source = inspect.getsource(tool_mod._account_document_ids)
    assert "while True" in source, "the fetch must loop over pages"
    assert "page += 1" in source
    assert "page_size" in source


def test_both_document_fetchers_agree_on_pagination():
    """The router and tool copies exist for different callers but must match.

    They were independent implementations and only one looped, which is exactly
    how the divergence went unnoticed.
    """
    import inspect

    from qwenpaw.agents.tools import knowledge as tool_mod
    from qwenpaw.app.routers import paths as router_mod

    for fn in (tool_mod._account_document_ids, router_mod._account_document_ids):
        source = inspect.getsource(fn)
        assert "while True" in source, f"{fn.__module__}.{fn.__name__} does not paginate"
