"""Orchestrator writes into a generator workspace without following its links.

The generator's sandbox lets it create symbolic links inside its workspace, while the
orchestrator writes there unsandboxed between generation turns. Every orchestrator
write therefore replaces a link instead of writing through it, and creates a new
file by rename, so a hard link to an outside file is never modified either.
No generation process runs while these helpers are used.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from osverifier.storage import atomic_text


def _parts(relative) -> tuple[str, ...]:
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(
        part in ("", ".", "..") for part in parts
    ):
        raise ValueError(f"invalid workspace path: {relative}")
    return parts


def _real_directory(workspace: Path, parts: tuple[str, ...]) -> Path:
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("generator workspace must be a real directory")
    current = workspace
    for part in parts:
        current = current / part
        if current.is_symlink():
            current.unlink()
        elif current.exists() and not current.is_dir():
            raise ValueError(f"workspace path is not a directory: {current}")
        current.mkdir(exist_ok=True)
    if not current.resolve().is_relative_to(workspace.resolve()):
        raise ValueError(f"workspace path escapes the workspace: {current}")
    return current


def real_directory(workspace: Path, relative) -> Path:
    """Return a directory reached only through real directories inside the workspace."""
    return _real_directory(workspace, _parts(relative))


def destination(workspace: Path, relative) -> Path:
    """A file path whose parents are real workspace directories and which is not a link."""
    parts = _parts(relative)
    target = _real_directory(workspace, parts[:-1]) / parts[-1]
    if target.is_symlink():
        target.unlink()
    elif target.exists() and not target.is_file():
        raise ValueError(f"workspace path is not a regular file: {target}")
    return target


def write_text(workspace: Path, relative, text: str) -> Path:
    target = destination(workspace, relative)
    atomic_text(target, text)
    return target


def _replace_from(target: Path, produce) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(fd)
    try:
        produce(temporary)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def copy_file(source: Path, workspace: Path, relative) -> Path:
    target = destination(workspace, relative)
    _replace_from(target, lambda temporary: shutil.copyfile(source, temporary))
    return target


def copy_tree(source: Path, workspace: Path, relative) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            copy_file(path, workspace, PurePosixPath(relative, *path.relative_to(source).parts))


def extract_pdf(binary: str, workspace: Path, relative, output_relative) -> None:
    source = destination(workspace, relative)
    target = destination(workspace, output_relative)
    _replace_from(
        target,
        lambda temporary: subprocess.run([binary, "-layout", str(source), temporary], check=True),
    )
