"""Local web GUI for Rethlas-OSV (the product name of the OSVerify core).

This package is deliberately separate from ``osverifier``: the core freezes a hash of
every ``.py``/``.md`` file under ``src/osverifier`` into each run's manifest, so the GUI
must be able to change without invalidating resumable runs. The GUI never calls the
verifier in-process; it launches the existing command-line entry points as child
processes, interrupts them with SIGINT exactly like Ctrl+C, and resumes by replaying
the stored command's validated arguments through the current entry point (see replay.py).
"""
