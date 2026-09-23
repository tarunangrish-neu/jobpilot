"""Helpers shared by the ATS adapters: HTML flattening, dates, remote detection."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Optional

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
}
_SKIP_TAGS = {"script", "style", "head"}

_REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\bdistributed\b|\banywhere\b", re.I)


class _TextExtractor(HTMLParser):
    """Flatten HTML to readable text using only the standard library.

    Job descriptions are the input to the visa screen and the embedder, so
    they need to be plain text with the list/paragraph breaks preserved --
    'No sponsorship' on its own <li> must not run into the previous line.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(raw: Optional[str]) -> str:
    """Convert a description field to plain text.

    Greenhouse double-encodes: the `content` field is HTML whose angle
    brackets arrive as &lt;/&gt; entities, so it must be unescaped *before*
    parsing or the whole document reads as literal text.
    """
    if not raw:
        return ""
    text = html.unescape(raw)
    if "<" in text:
        parser = _TextExtractor()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(ln for ln in lines if ln or True)).strip()


def looks_remote(*values: Optional[str]) -> bool:
    return any(_REMOTE_RE.search(v) for v in values if v)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to aware UTC.

    Tolerates a trailing Z and Recruitee's "2026-09-23 09:10:19 UTC".
    """
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    if text.endswith(" UTC"):
        text = text[:-4] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_epoch_ms(value) -> Optional[datetime]:
    """Parse Lever's millisecond epoch `createdAt` to aware UTC."""
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
