from agent_toolkit.email_html import (
    _clean_email_html,
    _ensure_email_html,
    _has_block_markup,
    build_quoted_reply_html,
    html_to_preview_text,
    plain_text_to_quoted_html,
)


def test_clean_email_html_strips_code_fences():
    raw = "```html\n<p>Hello.</p>\n```"
    assert _clean_email_html(raw) == "<p>Hello.</p>"


def test_clean_email_html_keeps_allowed_tags():
    raw = "<p>Hi <strong>Tom</strong>,</p><ul><li>One</li><li>Two</li></ul>"
    assert _clean_email_html(raw) == raw


def test_clean_email_html_strips_disallowed_tags_but_keeps_text():
    raw = '<div class="x">Hello <span onclick="evil()">world</span></div>'
    assert _clean_email_html(raw) == "Hello world"


def test_clean_email_html_strips_script_content_entirely():
    raw = "<p>Hi</p><script>alert(1)</script>"
    assert "alert" not in _clean_email_html(raw)


def test_clean_email_html_keeps_only_href_on_anchor():
    raw = '<a href="https://x.com" onclick="evil()" class="y">link</a>'
    assert _clean_email_html(raw) == '<a href="https://x.com">link</a>'


def test_has_block_markup_true_for_paragraph():
    assert _has_block_markup("<p>hi</p>") is True


def test_has_block_markup_false_for_plain_text():
    assert _has_block_markup("just some words") is False


def test_ensure_email_html_converts_plain_text_to_paragraphs():
    result = _ensure_email_html("Hello team,\n\nSee you Tuesday.")
    assert "<p>Hello team,</p>" in result
    assert "<p>See you Tuesday.</p>" in result


def test_ensure_email_html_converts_markdown_bullets():
    result = _ensure_email_html("- Bring water\n- Bring snacks")
    assert "<ul>" in result
    assert "<li>Bring water</li>" in result


def test_ensure_email_html_passthrough_when_already_block_html():
    raw = "<p>Already formatted</p>"
    assert _ensure_email_html(raw) == raw


def test_build_quoted_reply_html_escapes_attribution():
    out = build_quoted_reply_html(
        "<p>Reply text</p>", "<p>Original</p>", 'Shawn Lynch <lynchst@comcast.net>',
    )
    assert "&lt;lynchst@comcast.net&gt;" in out
    assert "<p>Reply text</p>" in out
    assert "<p>Original</p>" in out


def test_plain_text_to_quoted_html_preserves_line_breaks_and_escapes():
    out = plain_text_to_quoted_html("line one\n<script>evil()</script>")
    assert out == "line one<br>&lt;script&gt;evil()&lt;/script&gt;"


def test_html_to_preview_text_strips_tags_and_bullets():
    html = "<p>Hi</p><ul><li>One</li><li>Two</li></ul>"
    preview = html_to_preview_text(html)
    assert "<" not in preview
    assert "• One" in preview
    assert "• Two" in preview
