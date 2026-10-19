"""UTF-8 everywhere, whatever locale the process was started under.

Proofs and papers are full of non-ASCII mathematics. A child started from Finder, a
desktop launcher, or a terminal with a C or Latin-1 locale would otherwise encode the
prompt it pipes into Codex (``stream did not contain valid UTF-8``) or its own JSON
output with that locale and fail on the first such symbol.
"""

from __future__ import annotations

import locale
import os
import sys


def utf8_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment in which Python children run in UTF-8 mode."""
    env = dict(os.environ if base is None else base)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def text_encoding_warning() -> str | None:
    """Why text I/O in this process is not UTF-8, or None when it is."""
    if sys.flags.utf8_mode:
        return None
    preferred = locale.getpreferredencoding(False)
    if preferred.lower().replace("-", "").replace("_", "") == "utf8":
        return None
    return (
        f"text encoding is {preferred}, not UTF-8; mathematical symbols can fail to encode. "
        "Set PYTHONUTF8=1 or a UTF-8 locale (for example LANG=C.UTF-8) before running."
    )


def tolerate_console_encoding() -> None:
    """Never crash on a console that cannot show a character; replace it instead."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
