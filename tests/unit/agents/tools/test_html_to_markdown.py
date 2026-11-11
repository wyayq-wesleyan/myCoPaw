# -*- coding: utf-8 -*-
"""stdlib HTML→Markdown 转换器的行为测试。

为什么要有这组测试：``web_fetch`` 把网页转成文本喂给模型，转换结果直接进上下文。
这段实现替换掉了 GPL 许可的 ``html2text``（见 docs/上游修复移植论证.md §2.5），
所以既要保证"该保留的保留"（标题/链接/列表/表格/代码），也要保证"该丢的丢"
（脚本/样式/图片/伪协议链接），还要保证畸形与病态输入不会拖死进程。
"""

from __future__ import annotations

import time
from pathlib import Path

from qwenpaw.agents.tools._html_to_markdown import html_to_markdown
from qwenpaw.agents.tools.web_search import _html_to_text


# ---------------------------------------------------------------------------
# 该保留的
# ---------------------------------------------------------------------------


def test_headings_and_paragraphs() -> None:
    out = html_to_markdown("<h1>标题</h1><p>a</p><p>b</p>")
    assert "# 标题" in out
    assert "a" in out and "b" in out
    # 段落之间有空行（否则模型看到的是连成一句）
    assert "a\n\nb" in out


def test_links_are_kept() -> None:
    out = html_to_markdown('<a href="https://e.com/x">链接</a>')
    assert out == "[链接](https://e.com/x)"


def test_unordered_and_ordered_lists() -> None:
    assert html_to_markdown("<ul><li>a</li><li>b</li></ul>") == "* a\n* b"
    assert html_to_markdown('<ol start="3"><li>a</li></ol>') == "3. a"


def test_nested_list_is_indented() -> None:
    out = html_to_markdown("<ul><li>a<ul><li>b</li></ul></li></ul>")
    assert "* a" in out
    assert "  * b" in out


def test_table_cells_are_separated() -> None:
    out = html_to_markdown(
        "<table><tr><th>A</th><th>B</th></tr>"
        "<tr><td>1</td><td>2</td></tr></table>",
    )
    assert "| A | B |" in out
    assert "| 1 | 2 |" in out


def test_pre_block_uses_a_long_enough_fence() -> None:
    out = html_to_markdown('<pre><code>print("`")</code></pre>')
    # 内部有一个反引号，围栏必须是三个（>= 内部最长连续反引号 + 1）
    assert "```" in out
    assert 'print("`")' in out


def test_inline_code() -> None:
    assert html_to_markdown("<code>x</code>") == "`x`"


def test_entities_are_decoded() -> None:
    assert html_to_markdown("&amp; &lt; &#x4e2d;") == "& < 中"


def test_chinese_page_stays_readable() -> None:
    out = html_to_markdown(
        "<h2>关于我们</h2><p>这是第一段。</p><p>这是第二段。</p>",
    )
    assert "## 关于我们" in out
    assert "第一段" in out and "第二段" in out


# ---------------------------------------------------------------------------
# 该丢的
# ---------------------------------------------------------------------------


def test_images_are_dropped() -> None:
    out = html_to_markdown('<p>t</p><img src="x.png" alt="图">')
    assert "x.png" not in out
    assert "图" not in out


def test_script_style_and_title_are_dropped() -> None:
    out = html_to_markdown(
        "<title>不应该出现</title><script>alert(1)</script>"
        "<style>.a{color:red}</style><p>ok</p>",
    )
    assert "不应该出现" not in out
    assert "alert(1)" not in out
    assert "color:red" not in out
    assert "ok" in out


def test_fake_scheme_links_are_not_emitted_as_links() -> None:
    out = html_to_markdown('<a href="javascript:alert(1)">x</a>')
    assert "javascript:" not in out
    assert "x" in out


# ---------------------------------------------------------------------------
# 边界与健壮性
# ---------------------------------------------------------------------------


def test_empty_input() -> None:
    assert html_to_markdown("") == ""
    assert html_to_markdown("   \n\t ") == ""


def test_malformed_html_does_not_raise() -> None:
    assert "a" in html_to_markdown("<div><p>a")
    assert "x" in html_to_markdown("<b><i>x</b></i>")


def test_pathological_input_is_bounded() -> None:
    """1MB 单行 + 深嵌套必须很快返回，不能变成 CPU 黑洞。"""
    big = "<p>" + ("字" * (1024 * 1024)) + "</p>"
    nested = "<div>" * 2000 + "x" + "</div>" * 2000

    started = time.monotonic()
    html_to_markdown(big)
    html_to_markdown(nested)
    assert time.monotonic() - started < 2.0


# ---------------------------------------------------------------------------
# 接线：web_fetch 的入口仍然把 <title> 作为一级标题
# ---------------------------------------------------------------------------


def test_web_fetch_prepends_title() -> None:
    out = _html_to_text(
        "<html><head><title>页面标题</title></head>"
        "<body><p>正文</p></body></html>",
    )
    assert out.startswith("# 页面标题")
    assert "正文" in out


def test_web_fetch_without_title_returns_body() -> None:
    out = _html_to_text("<p>只有正文</p>")
    assert out == "只有正文"


# ---------------------------------------------------------------------------
# 许可守卫：不允许再引入 GPL 的 html2text
# ---------------------------------------------------------------------------


def test_html2text_is_not_imported_anywhere() -> None:
    root = Path(__file__).resolve().parents[3]
    offenders = []
    for path in (root / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "import html2text" in text or "from html2text" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"html2text (GPL-3.0) reintroduced: {offenders}"
