"""Rethlas's memory and retrieval tools; verification belongs to the Python loop.

Upstream semantics (problem-id sanitization, channels, record shapes, BM25 ranking) come
from the unmodified vendor module. Its file access is replaced: this server runs outside
the generator's sandbox, while the generator can create links in its workspace. Each
memory path component is therefore opened relative to its parent descriptor with
O_NOFOLLOW, and only regular, singly linked files are read or written.

No ``from __future__ import annotations`` here: fastmcp compares each tool's evaluated
type hints with its ``__annotations__`` and, when they differ (string annotations),
rebuilds the function with ``types.FunctionType``. Inside the compiled desktop bundle
the tools are Nuitka closures whose cells cannot be rebuilt, which killed the server
before it answered Codex's initialize request.
"""

import argparse
import errno
import importlib.util
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

TOOLS = ("memory_init", "memory_append", "memory_search", "branch_update")
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC


def load_upstream():
    source = Path(__file__).parent / "vendor/generation/mcp/server.py"
    spec = importlib.util.spec_from_file_location("rethlas_upstream_memory", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkspaceMemory:
    def __init__(self, upstream, workspace: Path):
        self.upstream = upstream
        self.workspace = workspace.resolve()
        self.root = self.workspace / "memory"

    @contextmanager
    def directory(self, sanitized: str, *, create: bool):
        """Yield a descriptor for memory/<problem id>, or None if absent and not created."""
        if create:
            self.workspace.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
        try:
            for part in ("memory", *sanitized.split("/")):
                if create:
                    try:
                        os.mkdir(part, 0o777, dir_fd=fd)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if create:
                        raise
                    child = None
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise ValueError(f"memory path {part!r} must be a real directory") from None
                    raise
                os.close(fd)
                fd = child
                if fd is None:
                    break
            yield fd
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def open(directory: int, name: str, flags: int, *, truncate=False) -> int:
        try:
            fd = os.open(name, flags | _NOFOLLOW, 0o666, dir_fd=directory)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENXIO):
                raise ValueError(f"memory file {name!r} must be a regular file") from None
            raise
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(fd)
            raise ValueError(f"memory file {name!r} must be a regular, singly linked file")
        os.set_blocking(fd, True)
        if truncate:
            os.ftruncate(fd, 0)
        return fd

    def channel_file(self, channel: str) -> str:
        files = self.upstream.CHANNEL_FILES
        if channel not in files:
            allowed = ", ".join(sorted(files))
            raise ValueError(f"Unknown channel '{channel}'. Allowed channels: {allowed}")
        return files[channel]

    def append(self, directory: int, name: str, payload: Dict[str, Any]) -> None:
        fd = self.open(directory, name, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def records(self, directory: int | None, name: str):
        if directory is None:
            return
        try:
            fd = self.open(directory, name, os.O_RDONLY)
        except FileNotFoundError:
            return
        with os.fdopen(fd, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    yield payload

    def memory_init(self, problem_id: str, meta: Optional[Dict[str, Any]] = None):
        upstream = self.upstream
        sanitized = upstream.sanitize_problem_id(problem_id)
        problem_dir = self.root / sanitized
        created_files: Dict[str, str] = {}
        with self.directory(sanitized, create=True) as directory:
            for channel, filename in upstream.CHANNEL_FILES.items():
                os.close(self.open(directory, filename, os.O_WRONLY | os.O_CREAT))
                created_files[channel] = str(problem_dir / filename)
            existing_meta: Dict[str, Any] = {}
            try:
                fd = self.open(directory, "meta.json", os.O_RDONLY)
            except FileNotFoundError:
                fd = None
            if fd is not None:
                with os.fdopen(fd, encoding="utf-8") as handle:
                    text = handle.read()
                if text:
                    loaded = json.loads(text)
                    if isinstance(loaded, dict):
                        existing_meta = loaded
            merged_meta: Dict[str, Any] = {
                "problem_id": sanitized,
                "created_at_utc": existing_meta.get("created_at_utc", upstream._utc_now()),
                "updated_at_utc": upstream._utc_now(),
            }
            merged_meta.update(existing_meta)
            if meta:
                merged_meta.update(meta)
            fd = self.open(directory, "meta.json", os.O_WRONLY | os.O_CREAT, truncate=True)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(merged_meta, handle, indent=2, ensure_ascii=False)
        return {
            "problem_id": sanitized,
            "memory_dir": str(problem_dir),
            "meta_path": str(problem_dir / "meta.json"),
            "channels": created_files,
        }

    def memory_append(self, problem_id: str, channel: str, record: Dict[str, Any]):
        upstream = self.upstream
        if not isinstance(record, dict):
            raise ValueError("record must be a JSON object")
        self.memory_init(problem_id)
        entry = {"timestamp_utc": upstream._utc_now(), "channel": channel, "record": record}
        filename = self.channel_file(channel)
        sanitized = upstream.sanitize_problem_id(problem_id)
        with self.directory(sanitized, create=True) as directory:
            self.append(directory, filename, entry)
            if channel != "events":
                event = {
                    "timestamp_utc": upstream._utc_now(),
                    "event_type": "memory_append",
                    "channel": channel,
                }
                self.append(directory, self.channel_file("events"), event)
        return {
            "status": "ok",
            "channel": channel,
            "path": str(self.root / sanitized / filename),
            "entry": entry,
        }

    def memory_search(self, problem_id, query, channels=None, limit_per_channel=10):
        upstream = self.upstream
        if not query.strip():
            raise ValueError("query must be non-empty")
        if limit_per_channel <= 0:
            raise ValueError("limit_per_channel must be > 0")
        if channels is None:
            search_channels = [name for name in upstream.CHANNEL_FILES if name != "events"]
        else:
            search_channels = channels
        sanitized = upstream.sanitize_problem_id(problem_id)
        results_by_channel: Dict[str, Dict[str, Any]] = {}
        total_results = 0
        with self.directory(sanitized, create=False) as directory:
            for channel in search_channels:
                items = list(self.records(directory, self.channel_file(channel)))
                documents = [json.dumps(item, ensure_ascii=False) for item in items]
                scores = upstream._bm25_score_documents(
                    query, [upstream._tokenize_bm25(document) for document in documents]
                )
                ranked_results: List[Dict[str, Any]] = []
                for item, score in sorted(
                    zip(items, scores),
                    key=lambda pair: (-pair[1], pair[0].get("timestamp_utc", "")),
                ):
                    if score <= 0:
                        continue
                    ranked_results.append({"score": score, "item": item})
                    if len(ranked_results) >= limit_per_channel:
                        break
                results_by_channel[channel] = {
                    "count": len(ranked_results),
                    "results": ranked_results,
                }
                total_results += len(ranked_results)
        return {
            "problem_id": sanitized,
            "query": query,
            "channels": search_channels,
            "limit_per_channel": limit_per_channel,
            "count": total_results,
            "results_by_channel": results_by_channel,
        }


def build_app(workspace: Path, allow_search: bool = True):
    from fastmcp import FastMCP

    upstream = load_upstream()
    memory = WorkspaceMemory(upstream, workspace)
    app = FastMCP(
        "rethlas-generation",
        instructions="Use these tools for experiment-local memory and mathematical retrieval. The Python orchestrator runs OSVerify after you submit a complete blueprint; there is no verification tool in this server.",
    )

    # Same names and parameters as the upstream functions they replace.
    def memory_init(
        problem_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return memory.memory_init(problem_id, meta)

    def memory_append(
        problem_id: str,
        channel: str,
        record: Dict[str, Any],
    ) -> Dict[str, Any]:
        return memory.memory_append(problem_id, channel, record)

    def memory_search(
        problem_id: str,
        query: str,
        channels: Optional[List[str]] = None,
        limit_per_channel: int = 10,
    ) -> Dict[str, Any]:
        return memory.memory_search(problem_id, query, channels, limit_per_channel)

    def branch_update(
        problem_id: str,
        branch_id: str,
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload = {"branch_id": branch_id, "state": state}
        return memory.memory_append(problem_id, "branch_states", payload)

    # As in upstream's own MCP wrapper: the model cannot choose the endpoint or timeout.
    def search_arxiv_theorems(
        query: str,
        num_results: int = 10,
    ) -> Dict[str, Any]:
        return upstream.search_arxiv_theorems(query=query, num_results=num_results)

    tools = [memory_init, memory_append, memory_search, branch_update]
    for tool in tools + ([search_arxiv_theorems] if allow_search else []):
        app.tool(name=tool.__name__)(tool)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    app = build_app(args.workspace, os.environ.get("RETHLAS_ALLOW_SEARCH", "1") == "1")
    app.run(show_banner=False)


if __name__ == "__main__":
    main()
