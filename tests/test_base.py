"""Tests for sources/base.py utilities."""

from jobscout.sources.base import html_to_text


def test_html_to_text_breaks_only_on_block_tags():
    html = "<p>Hello <b>world</b> today</p><p>Second paragraph</p>"
    text = html_to_text(html)
    assert "Hello world today" in text
    assert "Second paragraph" in text
    assert "Hello \nworld" not in text


def test_html_to_text_preserves_inline_content():
    html = "<span>Build</span> <a href='#'>APIs</a> with Python"
    text = html_to_text(html)
    assert "Build APIs with Python" in text.replace("\n", " ")
