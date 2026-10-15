"""Small shared helpers."""
from __future__ import annotations

import re
import secrets
from typing import Iterable

_TOKEN_PATTERNS = [
    re.compile(r"\b\d{7,11}:[A-Za-z0-9_-]{30,}\b"),  # telegram bot token
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}"),
]


def truncate(text: str, limit: int, marker: str = "\n...[truncated]...\n") -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(marker))
    head = keep * 2 // 3
    return text[:head] + marker + text[len(text) - (keep - head):]


def redact(text: str, secret_values: Iterable[str] = ()) -> str:
    """Mask configured secret values and anything that looks like a credential."""
    for s in secret_values:
        if s and len(s) >= 4:
            text = text.replace(s, "[REDACTED]")
    for rx in _TOKEN_PATTERNS:
        text = rx.sub("[REDACTED]", text)
    return text


def new_project_id() -> str:
    return "p" + secrets.token_hex(3)


def fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (s or "project")[:max_len].strip("-") or "project"
