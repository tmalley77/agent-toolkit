"""Generic HTML/text conversion helpers for email drafting.

Ported from ClaudeAIScoutMaster's app/email_html.py (see
ClaudeAIScoutMaster#277), itself split out of app/sdk_workers/email_draft.py
because these functions are pure string transforms with zero app
dependencies — unlike the rest of that module (content generation that
pulls in troop/Donna context and makes LLM calls). This is what unblocks
`agent_toolkit.gmail_client`'s quoted-reply rendering (Graph API's Outlook
path avoids needing this — createReply returns the quote server-side).
"""
import re
from html import escape as _html_escape

# Outlook's Graph API draft body renders as real HTML (contentType: HTML) —
# only tags in this safe subset are allowed through; everything else (script,
# style, span/class/style attributes, images) is stripped as defense-in-depth
# against an LLM ever echoing something it shouldn't into a sent email.
_ALLOWED_HTML_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u",
    "ul", "ol", "li", "a",
    "table", "thead", "tbody", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z0-9]+)([^>]*)>")


def _clean_email_html(text: str) -> str:
    """Strip markdown fences and reduce to the safe HTML tag subset.

    Keeps only tags in _ALLOWED_HTML_TAGS; `<a>` keeps only its `href`
    attribute, every other tag's attributes (class/style/onclick/etc.) are
    dropped. Disallowed tags (script, style, span, img, div...) are stripped
    but their inner text is kept, except script/style whose content is
    dropped entirely.
    """
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\n?```\s*$", "", text).strip()
    text = re.sub(r"(?is)<script.*?</script>", "", text)
    text = re.sub(r"(?is)<style.*?</style>", "", text)

    def _replace(m: "re.Match[str]") -> str:
        closing, tag, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if tag not in _ALLOWED_HTML_TAGS:
            return ""
        if tag == "a" and not closing:
            href = re.search(r'href\s*=\s*"([^"]*)"', attrs, re.IGNORECASE)
            return f'<a href="{href.group(1)}">' if href else "<a>"
        return f"<{'/' if closing else ''}{tag}>"

    return _TAG_RE.sub(_replace, text).strip()


# The sanitizer above only ever *removes* markup — it never produces any. A
# model that answers in plain text or markdown passes through byte-identical,
# and the body is then stored with an HTML content type where "\n" is
# insignificant whitespace, collapsing the whole draft into one wall of text.
# Everything below converts that fallback output into the same safe subset.
_BLOCK_TAG_RE = re.compile(r"(?i)<(p|ul|ol|li|table|tr|h[1-6]|br)\b")

# `[text](url)` first, bare http(s) URLs second — one pass with alternation so a
# markdown link's own URL can't be rewritten twice. The lookbehind keeps URLs
# already inside a surviving `<a href="...">` (or its anchor text) untouched.
_LINK_RE = re.compile(
    r"\[([^\]]+)\]\((https?://[^\s)]+)\)"
    r"|(?<![\"'>=])(https?://[^\s<>\"')]+)"
)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# Single `*` runs only when it isn't part of `**` and doesn't hug whitespace,
# so a bullet line ("* Item") can't be mistaken for emphasis.
_MD_ITALIC_RE = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)")
_BULLET_RE = re.compile(r"^\s*(?:[-•]|\*)\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.*)$")


def _has_block_markup(text: str) -> bool:
    """True if `text` already carries block-level HTML structure."""
    return bool(_BLOCK_TAG_RE.search(text))


def _link_sub(m: "re.Match[str]") -> str:
    if m.group(1):
        return f'<a href="{m.group(2)}">{m.group(1)}</a>'
    url = m.group(3)
    trail = ""
    while url and url[-1] in ".,;:!?":
        trail = url[-1] + trail
        url = url[:-1]
    return f'<a href="{url}">{url}</a>{trail}'


def _inline_markdown_to_html(text: str) -> str:
    """Convert inline markdown (links, bold, italic) to the safe tag subset."""
    text = _LINK_RE.sub(_link_sub, text)
    text = _MD_BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _MD_ITALIC_RE.sub(r"<em>\1</em>", text)
    return text


def _lines_to_html(lines: list) -> list:
    """Group consecutive same-kind lines into blocks.

    Runs of bullets become one <ul>, runs of numbered items one <ol>, and a run
    of ordinary lines one <p> with <br> between them (they were separated by a
    single newline, so they belong to the same paragraph).
    """
    out: list = []
    run: list = []
    run_kind = None

    def flush():
        nonlocal run, run_kind
        if not run:
            return
        if run_kind == "ul":
            out.append("<ul>" + "".join(f"<li>{_inline_markdown_to_html(t)}</li>" for t in run) + "</ul>")
        elif run_kind == "ol":
            out.append("<ol>" + "".join(f"<li>{_inline_markdown_to_html(t)}</li>" for t in run) + "</ol>")
        else:
            out.append("<p>" + "<br>".join(_inline_markdown_to_html(t) for t in run) + "</p>")
        run, run_kind = [], None

    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline_markdown_to_html(heading.group(2).strip())}</h{level}>")
            continue

        bullet = _BULLET_RE.match(line)
        numbered = None if bullet else _NUMBERED_RE.match(line)
        if bullet:
            kind, content = "ul", bullet.group(1)
        elif numbered:
            kind, content = "ol", numbered.group(1)
        else:
            kind, content = "p", line.strip()

        if kind != run_kind:
            flush()
            run_kind = kind
        run.append(content)

    flush()
    return out


def _plain_text_to_html(text: str) -> str:
    """Render plain-text / markdown email copy as safe-subset HTML."""
    html: list = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if lines:
            html.extend(_lines_to_html(lines))
    return "".join(html)


def _ensure_email_html(text: str) -> str:
    """Sanitize, then guarantee the result is real HTML.

    A no-op beyond sanitizing when the model already returned block-level
    markup; otherwise the plain-text/markdown fallback is converted so the
    body renders with paragraphs and lists instead of collapsing.
    """
    cleaned = _clean_email_html(text)
    if not cleaned or _has_block_markup(cleaned):
        return cleaned
    return _plain_text_to_html(cleaned)


def build_quoted_reply_html(body_html: str, original_html: str, attribution: str) -> str:
    """Your prose, then the original inside a standard quote block.

    `original_html` is inserted as-is so the quoted message keeps its own
    formatting. `attribution` is escaped: a sender rendered as
    `Shawn Lynch <lynchst@comcast.net>` would otherwise have the address
    swallowed as an unknown tag by the HTML renderer.
    """
    return (
        f"{body_html}"
        f'<div class="gmail_quote">'
        f'<div dir="ltr" class="gmail_attr">{_html_escape(attribution)}<br></div>'
        f'<blockquote class="gmail_quote" '
        f'style="margin:0 0 0 .8ex;border-left:1px #ccc solid;padding-left:1ex">'
        f"{original_html}"
        f"</blockquote></div>"
    )


def plain_text_to_quoted_html(text: str) -> str:
    """Escape plain text and keep its line breaks, for quoting an original that
    has no HTML part (or when only the stored text extract is available)."""
    return "<br>".join(_html_escape(line) for line in text.splitlines())


def html_to_preview_text(html: str) -> str:
    """Render safe-subset email HTML back to plain text for chat previews."""
    text = re.sub(r"(?i)</(p|li|h[1-6]|tr)>", "\n", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)<li>", "• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
