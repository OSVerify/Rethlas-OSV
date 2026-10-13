"""Launch, watch, interrupt, and reattach CLI processes, one per run directory."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from osverifier.storage import atomic_json, read_json

JOB_FILE = "gui-job.json"
STDOUT_LOG = "gui-stdout.log"
STDERR_LOG = "gui-stderr.log"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def lock_held(run_dir: Path) -> bool:
    """True when a verifier/prover process currently owns this run directory."""
    lock = run_dir / ".lock"
    if not lock.exists():
        return False
    with lock.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_job(run_dir: Path) -> dict | None:
    path = run_dir / JOB_FILE
    if not path.exists():
        return None
    try:
        return read_json(path)
    except ValueError:
        return None


class JobManager:
    """Owns child processes started by this GUI process and records their lifecycle.

    The record in ``gui-job.json`` survives GUI restarts. Children run in their own
    session, so a crashed or closed GUI never kills a review in progress; the next
    GUI instance reattaches through the recorded PID and the run lock.
    """

    def __init__(self):
        self._lock = threading.RLock()  # start/cancel re-enter through is_active
        self._processes: dict[Path, subprocess.Popen] = {}

    def is_active(self, run_dir: Path) -> bool:
        run_dir = run_dir.resolve()
        with self._lock:
            process = self._processes.get(run_dir)
        if process is not None:
            return process.poll() is None
        job = read_job(run_dir)
        if job is not None and job.get("status") == "running":
            if pid_alive(job.get("pid")) and lock_held(run_dir):
                return True
            # The GUI was not watching when the process ended; close the record.
            atomic_json(
                run_dir / JOB_FILE,
                {
                    **job,
                    "status": "exited",
                    "exit_code": None,
                    "finished_at": now(),
                    "note": "exit code not observed; the GUI was not running when the job ended",
                },
            )
            return False
        return lock_held(run_dir)

    def start(self, run_dir: Path, kind: str, argv: list[str], settings: dict, *, resume: bool):
        run_dir = run_dir.resolve()
        with self._lock:
            if self.is_active(run_dir):
                raise RuntimeError("this run is already in progress")
            run_dir.mkdir(parents=True, exist_ok=True)
            previous = read_job(run_dir) or {}
            record = {
                "kind": kind,
                "argv": argv,
                "settings": settings,
                "pid": None,
                "status": "starting",
                "started_at": now(),
                "finished_at": None,
                "exit_code": None,
                "cancel_requested_at": None,
                "invocations": int(previous.get("invocations", 0)) + 1,
                "resume": resume,
            }
            stdout = (run_dir / STDOUT_LOG).open("a")
            stderr = (run_dir / STDERR_LOG).open("a")
            stderr.write(
                f"\n--- invocation {record['invocations']} at {record['started_at']} ---\n"
            )
            stderr.flush()
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    cwd=str(run_dir),
                    start_new_session=True,
                )
            finally:
                stdout.close()
                stderr.close()
            record["pid"], record["status"] = process.pid, "running"
            atomic_json(run_dir / JOB_FILE, record)
            self._processes[run_dir] = process
        threading.Thread(
            target=self._watch, args=(run_dir, process), name=f"job-{run_dir.name}", daemon=True
        ).start()
        return record

    def _watch(self, run_dir: Path, process: subprocess.Popen):
        code = process.wait()
        with self._lock:
            self._processes.pop(run_dir, None)
            job = read_job(run_dir) or {}
            if job.get("pid") == process.pid:
                atomic_json(
                    run_dir / JOB_FILE,
                    {**job, "status": "exited", "exit_code": code, "finished_at": now()},
                )

    def cancel(self, run_dir: Path) -> dict:
        """Send SIGINT to the child's process group, the same signal as Ctrl+C.

        The child then cancels its own Codex process groups and records the
        interruption; completed job caches remain for a later resume.
        """
        run_dir = run_dir.resolve()
        with self._lock:
            process = self._processes.get(run_dir)
            job = read_job(run_dir) or {}
            pid = process.pid if process is not None else job.get("pid")
            if pid is None or not self.is_active(run_dir):
                raise RuntimeError("this run is not in progress")
            try:
                os.killpg(pid, signal.SIGINT)
            except ProcessLookupError as exc:
                raise RuntimeError("the run's process has already exited") from exc
            job = {**job, "cancel_requested_at": now()}
            atomic_json(run_dir / JOB_FILE, job)
            return job

    def shutdown_note(self) -> list[str]:
        """Runs still in progress when the GUI exits; they keep running on their own."""
        with self._lock:
            return [str(path) for path, p in self._processes.items() if p.poll() is None]
