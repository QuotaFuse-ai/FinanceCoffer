"""Keep the CLI's data streams UTF-8 whatever code page the host picked."""

from __future__ import annotations

import sys
from typing import Any

#: ``cp65001`` is Windows' numeric alias for UTF-8.
_UTF8_ALIASES = frozenset({"utf-8", "utf8", "utf_8", "cp65001"})


def ensure_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8 unless they already are.

    Windows gives a redirected process the ANSI/OEM code page (cp936 on a
    Chinese install), so ``fcoffer … --output json > file`` used to leave GBK
    bytes on disk and every UTF-8 reader downstream (jq, Node, CI) failed to
    decode them. A stream that cannot be reconfigured -- ``pythonw`` leaves
    stdout as ``None``, pytest capture and custom writers may omit
    ``reconfigure`` -- is left alone: the CLI must never fail because the
    diagnostic path is unavailable.
    """

    for stream in (sys.stdout, sys.stderr):
        _reconfigure_utf8(stream)


def _reconfigure_utf8(stream: Any) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    encoding = getattr(stream, "encoding", None)
    if isinstance(encoding, str) and encoding.lower() in _UTF8_ALIASES:
        return
    try:
        # ``replace`` keeps a lone surrogate in the payload from turning the
        # whole envelope into a UnicodeEncodeError; it never rewrites CJK text,
        # which UTF-8 represents natively.
        reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, TypeError, ValueError):
        return
