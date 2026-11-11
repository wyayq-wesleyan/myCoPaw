# -*- coding: utf-8 -*-
"""Tests for message_request_normalizer module."""

# pylint: disable=redefined-outer-name,protected-access
import json

import pytest
from agentscope.message import (
    Base64Source,
    DataBlock,
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    URLSource,
)

from qwenpaw.agents.utils.message_request_normalizer import (
    _clean_provider_specific_fields,
    _clone_msg,
    _clone_messages,
    _strip_media_blocks_in_place,
    normalize_messages_for_model_request,
)
from qwenpaw.constant import MEDIA_UNSUPPORTED_PLACEHOLDER


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _data_block(media_type: str, url: str = "file:///tmp/test") -> DataBlock:
    return DataBlock(source=URLSource(url=url, media_type=media_type))


def _is_media_block(block) -> bool:
    return getattr(block, "type", None) == "data"


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def text_message():
    return Msg(
        name="user",
        role="user",
        content=[TextBlock(text="Hello world")],
    )


@pytest.fixture
def image_message():
    return Msg(
        name="user",
        role="user",
        content=[_data_block("image/png", "file:///tmp/test.png")],
    )


@pytest.fixture
def video_message():
    return Msg(
        name="user",
        role="user",
        content=[_data_block("video/mp4", "file:///tmp/test.mp4")],
    )


@pytest.fixture
def audio_message():
    return Msg(
        name="user",
        role="user",
        content=[_data_block("audio/mpeg", "file:///tmp/test.mp3")],
    )


@pytest.fixture
def tool_result_with_image():
    return Msg(
        name="assistant",
        role="assistant",
        content=[
            ToolResultBlock(
                type="tool_result",
                id="call_1",
                name="view_image",
                output=[
                    {
                        "type": "data",
                        "source": {
                            "type": "url",
                            "url": "file:///tmp/result.png",
                            "media_type": "image/png",
                        },
                    },
                ],
            ),
        ],
    )


@pytest.fixture
def mixed_content_message():
    return Msg(
        name="user",
        role="user",
        content=[
            TextBlock(text="Look at this:"),
            _data_block("image/png", "file:///tmp/image.png"),
            TextBlock(text="And this video:"),
            _data_block("video/mp4", "file:///tmp/video.mp4"),
        ],
    )


# -----------------------------------------------------------------------------
# _clone_msg tests
# -----------------------------------------------------------------------------


def test_clone_msg_creates_independent_copy(text_message):
    cloned = _clone_msg(text_message)
    assert cloned.to_dict() == text_message.to_dict()
    assert cloned is not text_message
    assert cloned.content is not text_message.content


def test_clone_msg_modifications_dont_affect_original(text_message):
    cloned = _clone_msg(text_message)
    cloned.content[0].text = "Modified text"
    assert text_message.content[0].text == "Hello world"


# -----------------------------------------------------------------------------
# _clone_messages tests
# -----------------------------------------------------------------------------


def test_clone_messages_copies_list(text_message, image_message):
    msgs = [text_message, image_message]
    cloned = _clone_messages(msgs)
    assert len(cloned) == 2
    assert cloned[0].to_dict() == text_message.to_dict()
    assert cloned[1].to_dict() == image_message.to_dict()
    assert cloned[0] is not text_message
    assert cloned[1] is not image_message


# -----------------------------------------------------------------------------
# _strip_media_blocks_in_place tests
# -----------------------------------------------------------------------------


def test_strip_media_blocks_removes_image(image_message):
    msgs = [image_message]
    count = _strip_media_blocks_in_place(msgs)
    assert count == 1
    assert len(msgs[0].content) == 1
    assert msgs[0].content[0].type == "text"
    assert msgs[0].content[0].text == MEDIA_UNSUPPORTED_PLACEHOLDER


def test_strip_media_blocks_removes_video(video_message):
    msgs = [video_message]
    count = _strip_media_blocks_in_place(msgs)
    assert count == 1
    assert len(msgs[0].content) == 1
    assert msgs[0].content[0].type == "text"
    assert msgs[0].content[0].text == MEDIA_UNSUPPORTED_PLACEHOLDER


def test_strip_media_blocks_removes_audio(audio_message):
    msgs = [audio_message]
    count = _strip_media_blocks_in_place(msgs)
    assert count == 1
    assert len(msgs[0].content) == 1
    assert msgs[0].content[0].type == "text"
    assert msgs[0].content[0].text == MEDIA_UNSUPPORTED_PLACEHOLDER


def test_strip_media_blocks_handles_tool_result_with_media(
    tool_result_with_image,
):
    msgs = [tool_result_with_image]
    count = _strip_media_blocks_in_place(msgs)
    assert count == 1
    tool_result = msgs[0].content[0]
    assert tool_result.output == MEDIA_UNSUPPORTED_PLACEHOLDER


def test_strip_media_blocks_handles_mixed_content(mixed_content_message):
    msgs = [mixed_content_message]
    count = _strip_media_blocks_in_place(msgs)
    assert count == 2
    assert len(msgs[0].content) == 2
    assert msgs[0].content[0].type == "text"
    assert msgs[0].content[0].text == "Look at this:"
    assert msgs[0].content[1].type == "text"
    assert msgs[0].content[1].text == "And this video:"


def test_strip_media_blocks_preserves_non_media_content(text_message):
    msgs = [text_message]
    count = _strip_media_blocks_in_place(msgs)
    assert count == 0
    assert msgs[0].content == text_message.content


def test_strip_media_blocks_handles_empty_content():
    msg = Msg(name="user", role="user", content=[])
    msgs = [msg]
    count = _strip_media_blocks_in_place(msgs)
    assert count == 0
    assert msgs[0].content == []


def test_strip_media_blocks_handles_text_only_content():
    msg = Msg(
        name="user",
        role="user",
        content=[TextBlock(text="Plain text message")],
    )
    msgs = [msg]
    count = _strip_media_blocks_in_place(msgs)
    assert count == 0
    assert msgs[0].content[0].text == "Plain text message"


# -----------------------------------------------------------------------------
# normalize_messages_for_model_request tests
# -----------------------------------------------------------------------------


def test_normalize_with_multimodal_support_keeps_media(image_message):
    msgs = [image_message]
    normalized = normalize_messages_for_model_request(
        msgs,
        supports_multimodal=True,
    )
    assert msgs[0].content[0].type == "data"
    assert normalized[0].content[0].type == "data"
    assert normalized[0] is not msgs[0]


def test_normalize_without_multimodal_support_strips_media(image_message):
    msgs = [image_message]
    normalized = normalize_messages_for_model_request(
        msgs,
        supports_multimodal=False,
    )
    assert msgs[0].content[0].type == "data"
    assert normalized[0].content[0].type == "text"
    assert normalized[0].content[0].text == MEDIA_UNSUPPORTED_PLACEHOLDER


def test_normalize_preserves_original_messages(mixed_content_message):
    original_dict = mixed_content_message.to_dict()
    normalize_messages_for_model_request(
        [mixed_content_message],
        supports_multimodal=False,
    )
    assert mixed_content_message.to_dict() == original_dict


def test_normalize_returns_new_message_instances(text_message):
    msgs = [text_message]
    normalized = normalize_messages_for_model_request(
        msgs,
        supports_multimodal=True,
    )
    assert normalized[0] is not msgs[0]
    assert normalized[0].content is not msgs[0].content


# -----------------------------------------------------------------------------
# Integration tests with multiple messages
# -----------------------------------------------------------------------------


def test_normalize_conversation_with_multiple_messages():
    msgs = [
        Msg(
            name="user",
            role="user",
            content=[
                TextBlock(text="Hello"),
                _data_block("image/png", "file:///tmp/1.png"),
            ],
        ),
        Msg(
            name="assistant",
            role="assistant",
            content=[TextBlock(text="I see the image")],
        ),
        Msg(
            name="user",
            role="user",
            content=[_data_block("video/mp4", "file:///tmp/1.mp4")],
        ),
    ]

    normalized = normalize_messages_for_model_request(
        msgs,
        supports_multimodal=False,
    )

    assert len(normalized[0].content) == 1
    assert normalized[0].content[0].text == "Hello"

    assert len(normalized[1].content) == 1
    assert normalized[1].content[0].text == "I see the image"

    assert len(normalized[2].content) == 1
    assert normalized[2].content[0].text == MEDIA_UNSUPPORTED_PLACEHOLDER

    assert msgs[0].content[1].type == "data"
    assert msgs[2].content[0].type == "data"


# -----------------------------------------------------------------------------
# _clean_provider_specific_fields tests
# -----------------------------------------------------------------------------


@pytest.fixture
def tool_call_with_extra_content():
    """ToolCallBlock doesn't support extra_content as a native field,
    so we test with dict-style content where the function is applicable."""
    msg = Msg(
        name="assistant",
        role="assistant",
        content=[
            ToolCallBlock(
                type="tool_call",
                id="call_1",
                name="search",
                input=json.dumps({"q": "test"}),
            ),
        ],
    )
    return msg


def test_clean_handles_text_only_content():
    msg = Msg(
        name="user",
        role="user",
        content=[TextBlock(text="just text")],
    )
    _clean_provider_specific_fields([msg], "openai")
    assert msg.content[0].text == "just text"


def test_clean_handles_empty_list():
    _clean_provider_specific_fields([], "openai")


def test_clean_ignores_non_tool_blocks():
    msg = Msg(
        name="assistant",
        role="assistant",
        content=[
            TextBlock(text="hello"),
        ],
    )
    _clean_provider_specific_fields([msg], "openai")
    assert msg.content[0].text == "hello"


# -----------------------------------------------------------------------------
# normalize_messages_for_model_request with target_family tests
# -----------------------------------------------------------------------------


def _paired_tool_messages():
    """Create a tool_call + tool_result pair in one assistant message."""
    return [
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ToolCallBlock(
                    type="tool_call",
                    id="call_1",
                    name="foo",
                    input="{}",
                ),
                ToolResultBlock(
                    type="tool_result",
                    id="call_1",
                    name="foo",
                    output="ok",
                ),
            ],
        ),
    ]


def test_normalize_does_not_mutate_original_with_target_family():
    msgs = _paired_tool_messages()
    original_dicts = [m.to_dict() for m in msgs]
    normalize_messages_for_model_request(
        msgs,
        supports_multimodal=True,
        target_family="openai",
    )
    assert [m.to_dict() for m in msgs] == original_dicts


def test_raw_input_used_for_repair_before_stripping():
    """raw_input must be consumed by _repair_empty_tool_inputs before
    _clean_provider_specific_fields strips it.

    Scenario: tool_call with empty input but valid raw_input JSON.
    After normalize, input should be populated AND raw_input removed.
    """
    msgs = [
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ToolCallBlock(
                    type="tool_call",
                    id="call_repair",
                    name="run",
                    input="{}",
                ),
                ToolResultBlock(
                    type="tool_result",
                    id="call_repair",
                    name="run",
                    output="file.txt",
                ),
            ],
        ),
    ]

    normalized = normalize_messages_for_model_request(
        msgs,
        supports_multimodal=True,
        target_family="openai",
    )

    block = normalized[0].content[0]
    assert not hasattr(block, "raw_input") or not getattr(
        block,
        "raw_input",
        None,
    )


# -----------------------------------------------------------------------------
# 空媒体块（上游 #8066）
# -----------------------------------------------------------------------------


def _base64_block(data: str) -> DataBlock:
    return DataBlock(
        type="data",
        source=Base64Source(
            type="base64",
            media_type="image/png",
            data=data,
        ),
    )


def test_empty_media_block_is_dropped():
    """空 base64 会被序列化成空 data URI，所有 provider 都会 400。"""
    msg = Msg(
        name="user",
        role="user",
        content=[TextBlock(text="look"), _base64_block("")],
    )

    [normalized] = normalize_messages_for_model_request(
        [msg],
        supports_multimodal=True,
    )

    assert [b.type for b in normalized.content] == ["text"]


def test_non_empty_media_survives_multimodal_normalization():
    msg = Msg(
        name="user",
        role="user",
        content=[TextBlock(text="look"), _base64_block("AAAA")],
    )

    [normalized] = normalize_messages_for_model_request(
        [msg],
        supports_multimodal=True,
    )

    assert [b.type for b in normalized.content] == ["text", "data"]


def _tool_call_msg(call_id: str = "call-1") -> Msg:
    return Msg(
        name="assistant",
        role="assistant",
        content=[
            ToolCallBlock(
                id=call_id,
                name="view_image",
                input="{}",
                state="finished",
            ),
        ],
    )


def test_empty_media_inside_tool_result_is_dropped():
    msg = Msg(
        name="tool",
        role="assistant",
        content=[
            ToolResultBlock(
                id="call-1",
                name="view_image",
                output=[_base64_block(""), TextBlock(text="ok")],
                state="success",
            ),
        ],
    )

    normalized = normalize_messages_for_model_request(
        [_tool_call_msg(), msg],
        supports_multimodal=True,
    )

    output = normalized[-1].content[0].output
    assert [block.type for block in output] == ["text"]
    assert output[0].text == "ok"


def test_already_empty_tool_result_stays_empty():
    """没有可剥的东西时不能改成"媒体被移除"的占位符。"""
    msg = Msg(
        name="tool",
        role="assistant",
        content=[
            ToolResultBlock(
                id="call-1",
                name="demo",
                output=[],
                state="success",
            ),
        ],
    )

    normalized = normalize_messages_for_model_request(
        [_tool_call_msg(), msg],
        supports_multimodal=True,
    )

    assert normalized[-1].content[0].output == []


def test_url_media_is_not_mistaken_for_empty():
    """URL 源的"空"无法离线判断，不能误删。"""
    msg = Msg(
        name="user",
        role="user",
        content=[_data_block("image/png")],
    )

    [normalized] = normalize_messages_for_model_request(
        [msg],
        supports_multimodal=True,
    )

    assert [b.type for b in normalized.content] == ["data"]
