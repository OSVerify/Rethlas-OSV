"""Codex generation turns with JSON output, experiment-local MCP, and session resume."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from osverifier.backend import CodexBackend, stop_process
from osverifier.frozen import python_module_command
from osverifier.quota import communicate_with_quota_detection
from osverifier.storage import atomic_json, atomic_text, read_json

from .contracts import GenerationTurn, InvalidGenerationOutput


class CodexGenerator:
    def cancel(self):
        self.transport.cancel()

    def halt_for_quota(self, error):
        self.transport.halt_for_quota(error)

    def __init__(
        self,
        *,
        model,
        codex_bin="codex",
        codex_home=None,
        reasoning_effort="xhigh",
        timeout_seconds=None,
        agent_threads=4,
        agent_depth=2,
    ):
        if agent_threads < 1 or agent_depth < 0:
            raise ValueError("invalid generator subagent limits")
        self.transport = CodexBackend(
            model=model,
            codex_bin=codex_bin,
            codex_home=codex_home,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
        )
        self.agent_threads, self.agent_depth = agent_threads, agent_depth

    @property
    def identity(self):
        from importlib.metadata import version

        return {
            **self.transport.identity,
            "kind": "codex_generator",
            "sandbox": "workspace-write",
            "agent_threads": self.agent_threads,
            "agent_depth": self.agent_depth,
            "python": sys.executable,
            "fastmcp": version("fastmcp"),
        }

    def command(self, workspace, attempt, session_id, web_search):
        backend = self.transport
        memory_server = python_module_command("rethlas_osv.memory_server")
        command = [backend.codex_bin, "exec"]
        if session_id:
            command += ["resume", session_id]
        command += [
            "--ignore-user-config",
            *backend.connection.arguments,
            "--skip-git-repo-check",
            "--json",
            "--model",
            backend.model,
            "--output-schema",
            str(attempt / "schema.json"),
            "--output-last-message",
            str(attempt / "raw-output.json"),
        ]
        config = {
            "approval_policy": "never",
            "sandbox_mode": "workspace-write",
            # Independent-thinking turns also lose shell network access, not only search.
            "sandbox_workspace_write.network_access": web_search != "disabled",
            "model_reasoning_effort": backend.reasoning_effort,
            "web_search": web_search,
            "features.multi_agent": self.agent_depth > 0,
            "agents.max_threads": self.agent_threads,
            "agents.max_depth": max(1, self.agent_depth),
            "agents.subgoal-prover.description": "Develop an assigned Rethlas proof plan and report to the parent.",
            "agents.subgoal-prover.config_file": str(
                workspace / ".codex/agents/subgoal-prover.toml"
            ),
            "mcp_servers.rethlas.command": memory_server[0],
            "mcp_servers.rethlas.args": [*memory_server[1:], "--workspace", str(workspace)],
            "mcp_servers.rethlas.cwd": str(workspace),
            "mcp_servers.rethlas.required": True,
            "mcp_servers.rethlas.default_tools_approval_mode": "approve",
            "mcp_servers.rethlas.startup_timeout_sec": 30,
            "mcp_servers.rethlas.tool_timeout_sec": 120,
            "mcp_servers.rethlas.env.RETHLAS_ALLOW_SEARCH": "0"
            if web_search == "disabled"
            else "1",
        }
        for key, value in config.items():
            command += ["-c", f"{key}={json.dumps(value)}"]
        return command + ["-"]

    def generate(
        self, workspace: Path, attempt: Path, prompt: str, session_id: str | None, web_search: str
    ):
        attempt.mkdir(parents=True)
        atomic_json(attempt / "schema.json", GenerationTurn.model_json_schema())
        atomic_text(attempt / "prompt.txt", prompt)
        argv = self.command(workspace, attempt, session_id, web_search)
        atomic_json(
            attempt / "command.json",
            {
                "argv": self.transport.connection.redact(argv),
                "backend": self.identity,
                "session_id": session_id,
            },
        )
        env = os.environ.copy()
        if self.transport.codex_home:
            env["CODEX_HOME"] = str(self.transport.codex_home.expanduser().resolve())
        begun = time.monotonic()
        with (
            (attempt / "events.jsonl").open("w") as output,
            (attempt / "stderr.log").open("w") as error,
        ):
            # One transport owns every generation branch's active process group.
            # Quota/cancellation latches and process registration share this lock.
            process = self.transport.start_process(
                argv, cwd=workspace, env=env, stdout=output, stderr=error,
                cancelled_message="generation cancelled; start a fresh invocation to resume",
            )
            # Inside the try: an interrupt after registration still stops this group.
            try:
                atomic_json(
                    attempt / "process-start.json",
                    {"pid": process.pid, "started_at": datetime.now(timezone.utc).isoformat()},
                )
                communicate_with_quota_detection(
                    process, prompt, self.transport.timeout_seconds, attempt, self.halt_for_quota
                )
            except BaseException:
                stop_process(process)
                raise
            finally:
                self.transport.release_process(process)
                output.flush()
                self.transport.record_event_metadata(attempt)
                atomic_json(
                    attempt / "timing.json",
                    {"elapsed_seconds": time.monotonic() - begun, "returncode": process.returncode},
                )
        if process.returncode != 0:
            self.transport.raise_if_stopped("active generation branch cancelled")
            reported = read_json(attempt / "events-summary.json").get("error")
            raise RuntimeError(f"generator exited {process.returncode}: {reported}" if reported
                               else f"generator exited {process.returncode}; inspect {attempt}")
        metadata = read_json(attempt / "events-summary.json")
        ids = metadata["thread_ids"]
        if len(ids) != 1 or (session_id is not None and ids[0] != session_id):
            raise ValueError("generator did not return the expected unique session ID")
        try:
            raw = read_json(attempt / "raw-output.json")
        except FileNotFoundError:
            raise InvalidGenerationOutput(
                "the turn ended without the required final JSON response"
            ) from None
        except ValueError:
            raise InvalidGenerationOutput("the final response was not valid JSON") from None
        try:
            turn = GenerationTurn.model_validate(raw)
        except ValueError:
            raise InvalidGenerationOutput(
                "the final JSON response did not match the required schema "
                "(candidate_ready and a nonempty progress note)"
            ) from None
        return turn, ids[0]
