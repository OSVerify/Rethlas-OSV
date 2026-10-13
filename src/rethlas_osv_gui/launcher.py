"""Entry point of the desktop bundle.

``Rethlas-OSV --entry <module> ...`` runs that module's ``main()``; this is how the
bundle starts its own verifier, prover, paper-extraction, and memory-server helper
processes (see ``osverifier.frozen``). With no ``--entry``, it opens the desktop app.
"""

from __future__ import annotations

import importlib
import sys

from osverifier.frozen import ENTRY_FLAG, is_frozen

ENTRY_POINTS = {
    "osverifier.cli",
    "osverifier_reports.writer",
    "osverifier_reports.reference_tools",
    "rethlas_osv.cli",
    "rethlas_osv.memory_server",
    "osverifier_reports.paper_prepare",
    "rethlas_osv_gui.server",
}


def main(argv: list[str] | None = None) -> int:
    if is_frozen():
        # Source loaded from the bundle (the vendored MCP server) must not leave a
        # bytecode cache behind: any file added inside the app breaks its signature.
        sys.dont_write_bytecode = True
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == [ENTRY_FLAG]:
        if len(argv) < 2 or argv[1] not in ENTRY_POINTS:
            print(f"unknown entry point: {argv[1:2]}", file=sys.stderr)
            return 2
        module = importlib.import_module(argv[1])
        sys.argv = [f"{sys.argv[0]} {ENTRY_FLAG} {argv[1]}", *argv[2:]]
        result = module.main()
        return 0 if result is None else int(result)
    from rethlas_osv_gui.desktop import main as desktop_main

    return desktop_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
