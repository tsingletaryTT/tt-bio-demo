"""Unit tests for AffinityScorer.score()'s event emission.

Mirrors tests/unit/runner/test_folder_events.py's approach: exercise the real
`score()` method with `_score_real` monkeypatched out (the one seam this
module controls without hardware -- the actual nesso1 call is covered
separately, on real silicon, by tests/integration/test_affinity_real.py),
rather than testing a parallel pure model of the emit sequence. `score()`'s
own contract (never raise; always emit answer_start then answer_done/
answer_error) is exactly what Folder.fold() promises for job_start/job_done,
for the same reason: a wedged or erroring Q&A worker must not take the
daemon's driving thread down with it.
"""

import pytest

from runner.affinity import AffinityScorer


def test_score_emits_start_then_done(monkeypatch, tmp_path):
    scorer = AffinityScorer(device_id=0)
    monkeypatch.setattr(scorer, "_score_real", lambda input_path: {"score": 0.73})
    events = []
    scorer.score("q1", "dhfr", str(tmp_path / "in.yaml"), events.append)
    assert events[0] == {"type": "answer_start", "question_id": "q1",
                          "target_id": "dhfr"}
    assert events[1] == {"type": "answer_done", "question_id": "q1",
                          "target_id": "dhfr", "score": 0.73}


def test_score_emits_error_on_exception_and_never_raises(monkeypatch, tmp_path):
    scorer = AffinityScorer(device_id=0)

    def boom(_input_path):
        raise ValueError("bad ligand")

    monkeypatch.setattr(scorer, "_score_real", boom)
    events = []
    scorer.score("q1", "dhfr", str(tmp_path / "in.yaml"), events.append)
    assert events[-1]["type"] == "answer_error"
    assert events[-1]["question_id"] == "q1"
    assert events[-1]["target_id"] == "dhfr"
    assert "bad ligand" in events[-1]["message"]  # detail is fine here --
    # this is the RUNNER's event, not what the UI shows; the UI is what must
    # never display it verbatim (see spec's Global Constraints).


def test_score_never_raises_out_of_the_call(monkeypatch, tmp_path):
    scorer = AffinityScorer(device_id=0)
    monkeypatch.setattr(
        scorer, "_score_real",
        lambda _p: (_ for _ in ()).throw(RuntimeError("boom")))
    # Must not raise -- same "the runner must never crash the UI" rule
    # Folder.fold() follows. A crashed Q&A worker thread must not take the
    # daemon down.
    scorer.score("q1", "dhfr", str(tmp_path / "in.yaml"), lambda e: None)


def test_score_passes_the_input_path_through_to_score_real(monkeypatch, tmp_path):
    """The one thing `score()` must do besides bracket-and-forward: hand
    `_score_real` exactly the path it was given, unmodified. A regression
    here would silently score the wrong target."""
    scorer = AffinityScorer(device_id=0)
    seen = []

    def fake(input_path):
        seen.append(input_path)
        return {"score": 0.5}

    monkeypatch.setattr(scorer, "_score_real", fake)
    input_path = str(tmp_path / "affinity_dhfr.yaml")
    scorer.score("q1", "dhfr", input_path, lambda e: None)
    assert seen == [input_path]
