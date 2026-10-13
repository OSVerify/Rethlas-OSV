"""Rebuild a stored GUI command before resuming it; never execute stored argv as-is.

``gui-job.json`` lives inside the run folder, and a run folder may have been copied from
someone else. Resume therefore keeps only recorded arguments that this GUI could have
produced, checks each value, and launches them through the current interpreter or app
bundle. The executable is never taken from the file, which also keeps resume working
after the app bundle moved.
"""

from __future__ import annotations

import math
import os
import re
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

from osverifier.frozen import ENTRY_FLAG, python_module_command
from osverifier_reports.paper_prepare import MODES

from .settings import REASONING_EFFORTS, SEARCH_POLICIES, WEB_SEARCH_MODES

# The module each GUI command role launches. When an entry point moves, change it here
# and keep the old name in FORMER_MODULES so runs recorded by earlier releases resume.
ENTRY_MODULES = {
    "verify": "osverifier.cli",
    "paper": "osverifier_reports.paper_prepare",
    "prove": "rethlas_osv.cli",
}
FORMER_MODULES: dict[str, tuple[str, ...]] = {
    # Module paths of pre-release builds, before OSVerify and Rethlas-OSV became separate repositories.
    "paper": ("osverifier_gui.paper",),
    "prove": ("osverifier.integrations.rethlas.cli",),
}
ROLES_BY_KIND = {"verify": ("verify", "paper"), "prove": ("prove",)}


class ReplayError(ValueError):
    """The stored command is not one this GUI would have launched."""


class _Context:
    def __init__(self, run_dir: Path, codex_bins: Iterable[str], codex_homes: Iterable[str]):
        self.run_dir = run_dir.resolve()
        self.codex_bins = {b for b in codex_bins if b}
        self.codex_homes = {h for h in codex_homes if h is not None}


Check = Callable[[str, _Context], None]
SWITCH = object()  # flag without a value
LABELS = object()  # one or more LaTeX labels


def _int(minimum: int) -> Check:
    def check(value, context):
        if not re.fullmatch(r"\d{1,9}", value) or int(value) < minimum:
            raise ReplayError(f"expected an integer of at least {minimum}, got {value!r}")

    return check


def _float(minimum: float) -> Check:
    def check(value, context):
        try:
            number = float(value)
        except ValueError:
            number = math.nan
        if not math.isfinite(number) or number < minimum:
            raise ReplayError(f"expected a number of at least {minimum:g}, got {value!r}")

    return check


def _choice(choices: Iterable[str]) -> Check:
    allowed = tuple(choices)

    def check(value, context):
        if value not in allowed:
            raise ReplayError(f"expected one of {', '.join(allowed)}, got {value!r}")

    return check


def _text(value: str, context=None) -> None:
    if (
        not value.strip()
        or len(value) > 200
        or value.startswith("-")
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ReplayError(f"unexpected value {value!r}")


def _request_id(value: str, context) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", value):
        raise ReplayError(f"unexpected request id {value!r}")


def _absolute(value: str) -> Path:
    if not os.path.isabs(value):
        raise ReplayError(f"expected an absolute path, got {value!r}")
    return Path(value).resolve()


def _this_run(value: str, context: _Context) -> None:
    if _absolute(value) != context.run_dir:
        raise ReplayError(f"the stored command targets another run directory: {value}")


def _inside_run(value: str, context: _Context) -> None:
    if not _absolute(value).is_relative_to(context.run_dir):
        raise ReplayError(f"the stored command reads a file outside this run: {value}")


def _which(value: str) -> Path | None:
    found = shutil.which(value)
    return Path(found).resolve() if found else None


def _codex_bin(value: str, context: _Context) -> None:
    # A relative path would resolve inside the run folder, which the child uses as cwd.
    if os.sep in value and not os.path.isabs(value):
        raise ReplayError(f"the stored Codex binary is a relative path: {value!r}")
    if value in context.codex_bins:
        return
    resolved = _which(value)
    if resolved is not None and any(_which(b) == resolved for b in context.codex_bins):
        return
    raise ReplayError(
        f"this run was started with the Codex binary {value!r}, which is neither the one "
        "in Settings nor the bundled one; set Settings → Codex binary to it to resume"
    )


def _codex_home(value: str, context: _Context) -> None:
    if value in context.codex_homes:
        return
    if value and any(
        h and Path(h).expanduser().resolve() == Path(value).expanduser().resolve()
        for h in context.codex_homes
    ):
        return
    shown = value or "the default Codex account (none recorded)"
    raise ReplayError(
        f"this run uses the Codex account folder {shown}, which is not the one in Settings; "
        "set Settings → Codex account folder to it to resume"
    )


def _label(value: str, context=None) -> None:
    _text(value)


COMMON = {
    "--model": _text,
    "--codex-bin": _codex_bin,
    "--codex-home": _codex_home,
    "--reasoning-effort": _choice(REASONING_EFFORTS),
    "--timeout-seconds": _float(1),
    "--max-attempts": _int(1),
}
VERIFY = {
    **COMMON,
    "--question": _inside_run,
    "--solution": _inside_run,
    "--request-id": _request_id,
    "--run-dir": _this_run,
    "--web-search": _choice(WEB_SEARCH_MODES),
    "--workers": _int(1),
    "--max-issues": _int(1),
    "--review-type": _choice(("solution", "paper")),
    "--review-kind": _choice(("solution", "paper")),  # name used by earlier releases
    "--decompose": SWITCH,
    "--no-early-stop": SWITCH,
}
PAPER = {
    **COMMON,
    "--run-dir": _this_run,
    "--paper": _inside_run,
    "--mode": _choice(MODES),
    "--tags": LABELS,
}
PROVE = {
    "--problem": _inside_run,
    "--run-dir": _this_run,
    "--generator-model": _text,
    "--verifier-model": _text,
    "--generator-reasoning-effort": _choice(REASONING_EFFORTS),
    "--verifier-reasoning-effort": _choice(REASONING_EFFORTS),
    "--codex-bin": _codex_bin,
    "--codex-home": _codex_home,
    "--max-rounds": _int(1),
    "--generation-branches": _int(1),
    "--generator-timeout": _float(0),
    "--verifier-timeout": _float(1),
    "--search-policy": _choice(SEARCH_POLICIES),
    "--verifier-web-search": _choice(WEB_SEARCH_MODES),
    "--agent-threads": _int(1),
    "--agent-depth": _int(0),
    "--workers": _int(1),
    "--max-attempts": _int(1),
    "--max-issues": _int(1),
    "--root-only": SWITCH,
    "--no-early-stop": SWITCH,
}


def _arguments(tokens: list[str], spec: dict, context: _Context, role: str) -> list[str]:
    out, seen, index = [], set(), 0
    while index < len(tokens):
        flag = tokens[index]
        if flag not in spec:
            raise ReplayError(f"unexpected argument {flag!r} in the stored {role} command")
        if flag in seen:
            raise ReplayError(f"{flag} appears twice in the stored {role} command")
        seen.add(flag)
        out.append(flag)
        index += 1
        check = spec[flag]
        if check is SWITCH:
            continue
        if check is LABELS:
            start = index
            while index < len(tokens) and not tokens[index].startswith("-"):
                _label(tokens[index])
                out.append(tokens[index])
                index += 1
            if index == start:
                raise ReplayError(f"{flag} has no value in the stored {role} command")
            continue
        if index >= len(tokens):
            raise ReplayError(f"{flag} has no value in the stored {role} command")
        try:
            check(tokens[index], context)
        except ReplayError as exc:
            raise ReplayError(f"{flag}: {exc}") from None
        out.append(tokens[index])
        index += 1
    for required in ("--run-dir", "--codex-bin"):
        if required not in seen:
            raise ReplayError(f"the stored {role} command has no {required}")
    if "--codex-home" not in seen:
        _codex_home("", context)
    return out


def _entry(argv: list[str], roles: tuple[str, ...]) -> tuple[str, list[str]]:
    if len(argv) < 3 or argv[1] not in ("-m", ENTRY_FLAG):
        raise ReplayError("the stored command does not launch a GUI entry point")
    module = argv[2]
    for role in roles:
        if module == ENTRY_MODULES[role] or module in FORMER_MODULES.get(role, ()):
            return role, argv[3:]
    raise ReplayError(f"the stored command launches {module!r}, which this kind of run never uses")


def _verify(tokens: list[str], context: _Context) -> list[str]:
    if not tokens or tokens[0] != "verify":
        raise ReplayError("the stored verifier command is not `verify`")
    return [
        *python_module_command(ENTRY_MODULES["verify"]),
        "verify",
        *_arguments(tokens[1:], VERIFY, context, "verify"),
    ]


def rebuild_command(
    job: dict, run_dir: Path, *, codex_bins: Iterable[str], codex_homes: Iterable[str]
) -> list[str]:
    """The command to resume ``run_dir`` with, rebuilt from its validated record."""
    argv = job.get("argv") if isinstance(job, dict) else None
    kind = job.get("kind") if isinstance(job, dict) else None
    if kind not in ROLES_BY_KIND or not isinstance(argv, list):
        raise ReplayError("the stored job record is malformed")
    if not all(isinstance(a, str) for a in argv):
        raise ReplayError("the stored command is malformed")
    context = _Context(run_dir, codex_bins, codex_homes)
    role, rest = _entry(argv, ROLES_BY_KIND[kind])
    if role == "verify":
        return _verify(rest, context)
    if role == "prove":
        return [
            *python_module_command(ENTRY_MODULES["prove"]),
            *_arguments(rest, PROVE, context, "prove"),
        ]
    if "--" not in rest:
        raise ReplayError("the stored paper command has no verifier command")
    cut = rest.index("--")
    paper = _arguments(rest[:cut], PAPER, context, "paper")
    _, nested = _entry(rest[cut + 1 :], ("verify",))
    return [
        *python_module_command(ENTRY_MODULES["paper"]),
        *paper,
        "--",
        *_verify(nested, context),
    ]
