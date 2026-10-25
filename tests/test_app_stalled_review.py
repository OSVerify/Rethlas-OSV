
import pytest
from osverifier.storage import atomic_json, read_json
from test_rethlas import Generator, Reviewer, problem

from rethlas_osv.runner import Experiment, LoopConfig


@pytest.mark.parametrize('mode', ['partial', 'full_solution'])
def test_timeout_continuation_never_hands_off(tmp_path, mode):
    class TimeoutReviewer(Reviewer):
        def verify(self, request, directory):
            report = super().verify(request, directory)
            if report.run_status != 'completed':
                atomic_json(directory / 'jobs/proposer/attempt-0002/failure.json', {'type': 'BackendStalled'})
            return report
    p, run = problem(tmp_path), tmp_path / 'run'
    g = Generator([(mode == 'full_solution', 'Draft'), (True, 'Complete')])
    v = TimeoutReviewer([None, 'READY_FOR_HUMAN'])
    result = Experiment(g, v, LoopConfig(1)).run(p, run)
    assert result.status == 'budget_exhausted', result.error
    assert not (run / 'handoff.json').exists()
    first = read_json(run / 'rounds/0001/feedback.json')
    assert first['decision'] is None and first['run_status'] == 'incomplete' and first['mode'] == mode
    assert first['continuation_reason'] == 'verification_stalled'
    assert ('cannot hand off' in first['notice']) == (mode == 'full_solution')
    result = Experiment(g, v, LoopConfig(2)).run(p, run)
    assert result.status == 'ready_for_human', result.error
    assert len(g.calls) == 2 and 'verification_stalled' in g.calls[1]['prompt']
    assert (run / 'blueprint_ready_for_human.md').read_text() == 'Complete'


def test_resubmitted_full_draft_resumes_its_stalled_review(tmp_path):
    from test_rethlas_breadth import CachingReviewer

    class RetryReviewer(CachingReviewer):
        def verify(self, request, directory):
            report = super().verify(request, directory)
            if report.run_status != 'completed':
                atomic_json(directory / 'jobs/proposer/attempt-0002/failure.json', {'type': 'TimeoutExpired'})
            return report
    p, run = problem(tmp_path), tmp_path / 'run'
    g = Generator([(True, 'Same draft'), (True, 'Same draft')])
    v = RetryReviewer([None, 'READY_FOR_HUMAN'])
    result = Experiment(g, v, LoopConfig(2)).run(p, run)
    assert result.status == 'ready_for_human' and result.rounds_completed == 2, result.error
    # Both rounds reviewed the same draft in the same review directory.
    assert len(list((run / 'reviews').iterdir())) == 1 and len(v.calls) == 2
    assert (run / 'blueprint_ready_for_human.md').read_text() == 'Same draft'
    assert read_json(run / 'rounds/0001/stalled-report.json')['run_status'] == 'incomplete'


@pytest.mark.parametrize('failure', ['ValueError', 'BackendCancelled', 'QuotaExhausted'])
def test_other_failures_do_not_allow_continuation(tmp_path, failure):
    class FailedReviewer(Reviewer):
        def verify(self, request, directory):
            report = super().verify(request, directory)
            atomic_json(directory / 'jobs/proposer/attempt-0002/failure.json', {'type': failure})
            return report
    g = Generator([(False, 'Draft')])
    result = Experiment(g, FailedReviewer([None])).run(problem(tmp_path), tmp_path / 'run')
    assert result.status == 'incomplete' and len(g.calls) == 1








@pytest.mark.parametrize('decision', ['REVISE', 'REJECT'])
def test_stalled_review_preserves_audited_feedback_and_resume_receipt(tmp_path, decision):
    class StalledReviewer(Reviewer):
        def verify(self, request, directory):
            report = super().verify(request, directory)
            if request.mode == 'partial':
                report = report.model_copy(update={'run_status': 'incomplete'})
                atomic_json(directory / 'report.json', report.model_dump(mode='json'))
                atomic_json(directory / 'jobs/other/attempt-0002/failure.json', {'type': 'TimeoutExpired'})
            return report
    p, run = problem(tmp_path), tmp_path / 'run'
    g = Generator([(False, 'Partial'), (True, 'Complete')])
    v = StalledReviewer([decision, 'READY_FOR_HUMAN'])
    result = Experiment(g, v, LoopConfig(1)).run(p, run)
    assert result.status == 'budget_exhausted' and result.last_decision == decision
    feedback = read_json(run / 'rounds/0001/feedback.json')
    assert feedback['decision'] == decision and feedback['feedback'][0]['text'].startswith('Exact audited text')
    result = Experiment(g, v, LoopConfig(2)).run(p, run)
    assert result.status == 'ready_for_human', result.error
    assert len(v.calls) == 2  # Do not retry historical stalled reviews on loop resume.
    assert f'"decision": "{decision}"' in g.calls[1]['prompt']




def test_later_retry_of_same_draft_keeps_historical_stall_snapshot(tmp_path):
    from test_rethlas_breadth import CachingReviewer

    class RetryReviewer(CachingReviewer):
        def verify(self, request, directory):
            report = super().verify(request, directory)
            if report.run_status != 'completed':
                atomic_json(directory / 'jobs/proposer/attempt-0002/failure.json', {'type': 'BackendStalled'})
            return report
    p, run = problem(tmp_path), tmp_path / 'run'
    g = Generator([(False, 'Same draft'), (False, 'Same draft'), (True, 'Complete')])
    v = RetryReviewer([None, 'PASS', 'READY_FOR_HUMAN'])
    assert Experiment(g, v, LoopConfig(2)).run(p, run).status == 'budget_exhausted'
    result = Experiment(g, v, LoopConfig(3)).run(p, run)
    assert result.status == 'ready_for_human', result.error
    assert len(v.calls) == 3
    assert read_json(run / 'rounds/0001/stalled-report.json')['run_status'] == 'incomplete'
