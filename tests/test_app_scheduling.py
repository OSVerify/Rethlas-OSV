"""Real overlapping fixture calls, deterministic priority, cancellation and resume."""


from osverifier.workflow import DecomposingVerifier, WorkflowConfig
from workflow_fixtures import request


def verify(backend, directory, workers=2, early_stop=True):
    return DecomposingVerifier(
        backend, WorkflowConfig(workers=workers, max_attempts=1, early_stop_on_reject=early_stop)
    ).verify(request(), directory)
























def test_all_entry_points_default_to_sixteen_and_allow_overrides():
    from osverifier.cli import parser as verify_parser
    from osverifier_reports.ocr import parser as pdf_parser

    from rethlas_osv.cli import parser as rethlas_parser
    from rethlas_osv_gui.settings import DEFAULTS

    cases = [
        (
            verify_parser(),
            ["verify", "--request", "input.json", "--run-dir", "out", "--model", "fixture"],
        ),
        (rethlas_parser(), ["--run-dir", "out"]),
        (
            pdf_parser(),
            [
                "verify-pdf",
                "--ocr-manifest",
                "input.json",
                "--run-dir",
                "out",
                "--model",
                "fixture",
            ],
        ),
    ]
    for parser, argv in cases:
        assert parser.parse_args(argv).workers == 16
        assert parser.parse_args([*argv, "--workers", "3"]).workers == 3
    assert DEFAULTS["verify"]["workers"] == DEFAULTS["prove"]["workers"] == 16
