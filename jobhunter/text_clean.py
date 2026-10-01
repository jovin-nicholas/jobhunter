"""HTML job descriptions to plain text."""
from __future__ import annotations

import html

from bs4 import BeautifulSoup

_BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6"]


def html_to_text(markup: str) -> str:
    soup = BeautifulSoup(markup or "", "html.parser")
    # Line breaks only at block boundaries; inline tags (span, b, a) must not split a sentence.
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.append("\n")
    lines = (" ".join(line.split()) for line in soup.get_text().splitlines())
    return "\n".join(line for line in lines if line)


def repair_text(text: str | None) -> str:
    """HTML entities decoded, and UTF-8 that was read as Latin-1 or cp1252 ("Engineer \u00e2\u0080\u0094 Cloud") decoded
    back. The repair is kept only when the whole text decodes cleanly, so ordinary accents stay as they are."""
    if text is None:
        return ""
    text = html.unescape(text if isinstance(text, str) else str(text))
    if any(marker in text for marker in ("\u00e2", "\u00c3", "\u00c2")):
        for wrong in ("latin-1", "cp1252"):
            try:
                return text.encode(wrong).decode("utf-8")
            except UnicodeError:
                continue
    return text
