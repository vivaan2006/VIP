"""Small helpers for reading BEA's parameter-value lists, shared by the download and metadata code."""

from __future__ import annotations

import html
import re

_TAGS = re.compile(r"<[^>]+>")


def as_list(x) -> list:
    """BEA returns a single item as a dict instead of a one-item list. Always give back a list."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def key_of(value) -> str:
    """The code of one parameter value. Field names differ by dataset (Key, key, TableName, ...)."""
    if not isinstance(value, dict):
        return str(value).strip()
    lower = _lower(value)
    for k in ("key", "tablename", "tableid", "code"):
        if lower.get(k) not in (None, ""):
            return str(lower[k]).strip()
    return str(next(iter(value.values()), "")).strip()


def desc_of(value) -> str:
    """The description of one parameter value, cleaned up."""
    if not isinstance(value, dict):
        return ""
    lower = _lower(value)
    for k in ("desc", "description", "value"):
        if lower.get(k):
            return clean_text(lower[k])
    return ""


def clean_text(text) -> str:
    """Strip HTML tags/entities, fix mis-decoded characters, squash whitespace."""
    return " ".join(html.unescape(_TAGS.sub("", _fix_mojibake(str(text)))).split())


def safe_name(x: str) -> str:
    """A string that is safe to use as a file name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(x))[:80] or "item"


def _lower(d: dict) -> dict:
    return {str(k).strip().lower(): v for k, v in d.items()}


def _fix_mojibake(text: str) -> str:
    """UTF-8 text that was read as Latin-1 (e.g. 'â\x80\x99' for an apostrophe), from older cached files."""
    if "â\u0080" in text or "Ã" in text:
        try:
            return text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return text
