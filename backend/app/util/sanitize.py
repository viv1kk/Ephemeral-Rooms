"""Text sanitization for every user-supplied string that is stored or displayed.

Spec section 21.2 requires the same treatment for filenames, display names, and
document names alike: NFC normalization, control and directional-override
characters stripped, trimmed, length-capped. These strings are always rendered
as text and never as HTML; escaping is React's job, not this module's.

Note what this is *not*: it is not path safety. No sanitized string ever reaches
the filesystem layer, which builds paths only from server-generated UUIDs.
"""

from __future__ import annotations

import unicodedata

# Bidirectional overrides and embeddings. These can make "report.txt.exe" render
# as "report.exe.txt", so they are removed rather than escaped.
_DIRECTIONAL = {
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069",
    "\u200e", "\u200f", "\u061c",
}


def _strip_unsafe(text: str) -> str:
    out = []
    for ch in text:
        if ch in _DIRECTIONAL:
            continue
        # C0 (except nothing: even tab and newline are meaningless in a name)
        # and C1 control characters.
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        out.append(ch)
    return "".join(out)


def clean_text(raw: str, *, max_chars: int, fallback: str = "") -> str:
    """Normalize, strip, trim, and cap. Returns `fallback` if nothing survives."""
    if not isinstance(raw, str):
        return fallback
    text = unicodedata.normalize("NFC", raw)
    text = _strip_unsafe(text)
    # Collapse runs of whitespace so a name cannot be padded into a visual gap.
    text = " ".join(text.split())
    text = text[:max_chars].strip()
    return text or fallback


def clean_display_name(raw: str, *, max_chars: int, fallback: str) -> str:
    return clean_text(raw, max_chars=max_chars, fallback=fallback)


def clean_document_name(raw: str, *, max_chars: int) -> str:
    return clean_text(raw, max_chars=max_chars, fallback="untitled")


def clean_filename(raw: str, *, max_chars: int) -> str:
    """Sanitize a filename for display and for Content-Disposition.

    Path separators and the special names `.` and `..` are neutralized even
    though the result never becomes a path, so that a client which naively
    joins it to a local directory is not led anywhere surprising."""
    name = clean_text(raw, max_chars=max_chars, fallback="file")
    name = name.replace("/", "_").replace("\\", "_")
    if name in (".", ".."):
        return "file"
    return name or "file"
