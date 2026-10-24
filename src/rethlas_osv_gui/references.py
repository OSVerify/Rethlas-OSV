"""Reference files that a prove job gives to the generator and the verifier.

The page sends the bytes of the files the user chose; nothing here reads a path the user
typed. The files are written into the run folder because the runner hashes its
``--references`` directory again on every resume, and resume replays only paths inside
the run (``replay.py``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import shutil
import unicodedata
from pathlib import Path, PurePosixPath

DIRECTORY = "references"  # inside the run's gui-inputs/
MAX_FILES = 200
MAX_BYTES = 256 * 2**20


def _name(name) -> str:
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > 200
        or name.startswith(".")
        or "/" in name
        or "\\" in name
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise ValueError(f"unsupported reference file name {name!r}; rename the file")
    return name


def _key(relative: str) -> str:
    # macOS file systems ignore case and Unicode normalization when they compare names.
    return unicodedata.normalize("NFC", relative).casefold()


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def decode_uploads(entries) -> dict[str, bytes]:
    """``{name: bytes}`` from the page's ``[{"name": ..., "data": <base64>}]``."""
    if entries is None:
        return {}
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise ValueError(f"references must be a list of at most {MAX_FILES} files")
    files: dict[str, bytes] = {}
    keys, total = set(), 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("data"), str):
            raise ValueError("each reference file needs a name and its data")
        name = _name(entry.get("name"))
        if _key(name) in keys:
            raise ValueError(f"two reference files are named {name}")
        try:
            data = base64.b64decode(entry["data"], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"reference file {name} was not received intact") from None
        if not data:
            raise ValueError(f"reference file {name} is empty")
        total += len(data)
        if total > MAX_BYTES:
            raise ValueError(f"reference files exceed {MAX_BYTES // 2**20} MiB in total")
        keys.add(_key(name))
        files[name] = data
    return files


def recorded(run_dir: Path, provenance: dict) -> dict[str, Path]:
    """An earlier run's frozen reference files, each checked against its manifest."""
    listed = provenance.get("references") or {}
    if not isinstance(listed, dict):
        raise ValueError("the earlier run's manifest does not list its references")
    root = (run_dir / "inputs" / "references").resolve()
    files = {}
    for relative, expected in listed.items():
        parts = PurePosixPath(relative).parts if isinstance(relative, str) else ()
        if not parts or relative.startswith("/") or any(p in (".", "..") for p in parts):
            raise ValueError(f"the earlier run's manifest lists an unexpected reference {relative!r}")
        path = root.joinpath(*parts)
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root)
            or _sha256(path) != expected
        ):
            raise ValueError(f"the earlier run's reference {relative} no longer matches its manifest")
        files[relative] = path
    return files


def combine(uploads: dict[str, bytes], kept: dict[str, Path]) -> dict[str, bytes | Path]:
    """Kept files plus new uploads; a name may appear twice only with identical content."""
    files: dict[str, bytes | Path] = dict(kept)
    by_key = {_key(relative): relative for relative in kept}
    folders = {_key(str(parent)) for relative in kept for parent in PurePosixPath(relative).parents}
    for name, data in uploads.items():
        same = by_key.get(_key(name))
        if _key(name) in folders:
            raise ValueError(f"{name} is also a folder among the earlier run's references; rename the file")
        if same is None:
            files[name] = data
        elif hashlib.sha256(data).hexdigest() != _sha256(kept[same]):
            raise ValueError(
                f"{name} differs from the earlier run's reference {same}; rename one of them "
                "or do not keep the earlier run's references"
            )
    return files


def write(directory: Path, files: dict[str, bytes | Path]) -> Path | None:
    """Write the files into ``directory`` and return it, or None when there are none."""
    if not files:
        return None
    for relative, source in files.items():
        target = directory.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(source, Path):
            shutil.copyfile(source, target)
        else:
            target.write_bytes(source)
    return directory
