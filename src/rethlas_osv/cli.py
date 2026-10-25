from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from osverifier.backend import DEFAULT_MODEL, CodexBackend
from osverifier.pipeline import RunConfig, Verifier
from osverifier.workflow import DecomposingVerifier, WorkflowConfig

from .encoding import text_encoding_warning, tolerate_console_encoding
from .generator import CodexGenerator
from .runner import ROOT, Experiment, LoopConfig


def parser():
    p = argparse.ArgumentParser(
        description="Run Rethlas generation with OSVerify feedback and a human handoff."
    )
    p.add_argument("--problem", type=Path, default=ROOT / "vendor/generation/data/example.md")
    p.add_argument(
        "--references",
        type=Path,
        help="Optional directory; defaults to the problem's sibling .refs directory if present. "
        "The generator and the verifier's proposer, validator and auditor can read these files",
    )
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--generator-model", default=DEFAULT_MODEL, help=f"default {DEFAULT_MODEL}; `osverify models` lists choices")
    p.add_argument("--verifier-model", default=DEFAULT_MODEL, help=f"default {DEFAULT_MODEL}")
    p.add_argument("--generator-reasoning-effort", default="xhigh")
    p.add_argument("--verifier-reasoning-effort", default="xhigh")
    p.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", "codex"))
    p.add_argument("--codex-home", type=Path)
    p.add_argument(
        "--max-rounds",
        type=int,
        default=10,
        help="Generation rounds per branch; may be increased on resume",
    )
    p.add_argument(
        "--generation-branches",
        type=int,
        default=1,
        help="Independent candidate sessions generated in parallel each round (default: 1)",
    )
    p.add_argument(
        "--generator-timeout",
        type=float,
        default=0,
        help="Optional time limit per generation turn in seconds; 0 disables it (default)",
    )
    p.add_argument("--verifier-idle-timeout", type=float, default=300, help="Retry a verifier call after this many seconds without item progress; 0 disables (default: 300). Silent reasoning can also reach this operational limit.")
    p.add_argument("--verifier-timeout", type=float, default=900, help="Per verifier role/attempt")
    p.add_argument(
        "--search-policy",
        choices=["alternating", "live", "disabled"],
        default="alternating",
        help="Generator search policy; verifier search is independently configured",
    )
    p.add_argument("--verifier-web-search", choices=["disabled", "cached", "live"], default="live")
    p.add_argument("--agent-threads", type=int, default=4)
    p.add_argument("--agent-depth", type=int, default=2, help="0 disables generation subagents")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--max-issues", type=int, default=None)
    p.add_argument("--max-attempts", type=int, default=2)
    p.add_argument(
        "--verifier-timeout-retries",
        type=int,
        default=1,
        help="Extra attempts a verifier call may earn by timing out; retries after a timeout "
        "get doubled time limits (default: 1)",
    )
    p.add_argument(
        "--root-only",
        action="store_true",
        help="Use the legacy whole-solution verifier; default includes decomposition",
    )
    p.add_argument(
        "--no-early-stop",
        action="store_true",
        help="Continue reviewing remaining issues and segments after an audited REJECT; "
        "incompatible with --root-only; READY_FOR_HUMAN still stops the experiment",
    )
    p.add_argument(
        "--seed-from",
        type=Path,
        metavar="RUN_DIR",
        help="Start this new run from another run's latest draft of the same problem: round 1 "
        "reviews that draft with this run's references, then generation continues in a fresh "
        "session. The other run is only read",
    )
    p.add_argument(
        "--seed-branch",
        type=int,
        help="With --seed-from: the branch to take the draft from when that run has several",
    )
    p.add_argument(
        "--prepare-only",
        action="store_true",
        help="Freeze inputs and prepare the workspace without model calls",
    )
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    tolerate_console_encoding()
    warning = text_encoding_warning()
    if warning:
        print(f"warning: {warning}", file=sys.stderr, flush=True)
    try:
        if args.root_only and args.no_early_stop:
            raise ValueError("--no-early-stop cannot be combined with --root-only")
        generator = CodexGenerator(
            model=args.generator_model,
            codex_bin=args.codex_bin,
            codex_home=args.codex_home,
            reasoning_effort=args.generator_reasoning_effort,
            timeout_seconds=args.generator_timeout or None,
            agent_threads=args.agent_threads,
            agent_depth=args.agent_depth,
        )
        backend = CodexBackend(
            model=args.verifier_model,
            codex_bin=args.codex_bin,
            codex_home=args.codex_home,
            reasoning_effort=args.verifier_reasoning_effort,
            timeout_seconds=args.verifier_timeout,
            idle_timeout_seconds=args.verifier_idle_timeout or None,
            web_search=args.verifier_web_search,
        )
        config = dict(
            workers=args.workers, max_issues=args.max_issues, max_attempts=args.max_attempts,
            timeout_retries=args.verifier_timeout_retries,
        )
        verifier = (
            Verifier(backend, RunConfig(**config))
            if args.root_only
            else DecomposingVerifier(
                backend, WorkflowConfig(**config, early_stop_on_reject=not args.no_early_stop)
            )
        )
        references = args.references
        if references is None and args.problem.with_suffix(".refs").is_dir():
            references = args.problem.with_suffix(".refs")
        experiment = Experiment(
            generator,
            verifier,
            LoopConfig(args.max_rounds, args.search_policy, args.generation_branches),
            event_sink=lambda row: print(json.dumps(row, ensure_ascii=False), flush=True),
        )
        result = experiment.run(
            args.problem,
            args.run_dir,
            references,
            prepare_only=args.prepare_only,
            seed_from=args.seed_from,
            seed_branch=args.seed_branch,
        )
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2), flush=True)
        if args.prepare_only and result.status == "running":
            return 0
        return {"ready_for_human": 0, "budget_exhausted": 3}.get(result.status, 2)
    except KeyboardInterrupt:
        print("Interrupted; resume with the same run directory.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
