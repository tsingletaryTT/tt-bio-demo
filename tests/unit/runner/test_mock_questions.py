"""End-to-end proof that `--mock` replay (runner/mock.py) can serve a stream
containing an affinity question/answer pair, so UI development and CI both
exercise this feature with no hardware and no nesso1 model load.

No code change was needed to make this pass. `MockRunner._serve` (runner/
mock.py) replays whatever `protocol.events.encode` accepts, and `encode`
accepts anything in `EVENT_TYPES` -- which already lists `answer_start`,
`answer_done` and `answer_error` (added to the protocol in Task 2, for this
same plan). So the only thing this task's Step 3 needed was the fixture
file, not a code branch -- exactly the outcome the task brief predicted as
"likely", by the same reasoning Task 8 used for `card_state`.

DELIBERATELY placed here (tests/unit/runner/), not in tests/integration/ as
the task brief's own file listing suggested. scripts/test.sh's own directory
rule treats every file under tests/integration/ as hardware-gated: it is
skipped by default and only runs with the `--hw` opt-in (see that script's
"hardware opt-in" comment block) -- deliberately, because that directory
opens real Tenstorrent cards. Putting a test THAT NEEDS NO HARDWARE there
would make it invisible to the very "UI development and CI both work with
no hardware" goal this task's own brief states as the point of the change.
This file needs no `tt_device`/`tt_cards_present` fixture and no card: it
exercises the replay-over-a-socket path only, which is exactly the point of
`--mock` existing at all (see runner/mock.py's own module docstring: "lets
the entire UI be built and exercised with no Tenstorrent hardware present").
tests/unit/runner/ is scripts/test.sh's actual "runs under venv-runner, no
hardware needed" bucket, so that is where this belongs.
"""

import pathlib
import socket

from protocol.events import decode
from runner.mock import MockRunner, load_stream

FIXTURE = pathlib.Path("tests/fixtures/streams/with_question.jsonl")

# The fixture's own cif is checked into the repo (tests/fixtures/structures/
# dhfr_with_ligand.cif) rather than referencing a path outside it -- same
# reasoning as test_affinity_real.py's vendored-input assert: a missing
# fixture is a problem with this checkout, not a legitimate skip.
assert FIXTURE.is_file(), f"fixture stream is missing: {FIXTURE}"


def _replay_all(sock_path):
    """Connect to the mock runner's socket and drain every event to EOF --
    the same helper tests/unit/test_mock_runner.py already uses."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(sock_path)
    with client.makefile("rb") as stream:
        return [decode(line) for line in stream]


def test_mock_runner_replays_a_question_answer_pair(tmp_path):
    sock_path = str(tmp_path / "runner.sock")
    runner = MockRunner(sock_path, load_stream(FIXTURE), speed=100.0)
    runner.start()
    try:
        events = _replay_all(sock_path)
    finally:
        runner.stop()

    types = [e["type"] for e in events]
    assert "answer_start" in types
    assert "answer_done" in types


def test_the_stream_replays_in_the_order_a_real_daemon_would_produce(tmp_path):
    """Specifically: the question is answered AFTER the fold that grounds
    it finishes (`job_done`), not mid-fold -- the order
    `ui.app._maybe_highlight_pocket` assumes (a visitor can only ask about
    a structure that is already on screen). A generic replay test could
    pass on any ordering; this one pins the ordering that actually matters
    for the pocket highlight to have somewhere real to land."""
    sock_path = str(tmp_path / "runner.sock")
    runner = MockRunner(sock_path, load_stream(FIXTURE), speed=100.0)
    runner.start()
    try:
        events = _replay_all(sock_path)
    finally:
        runner.stop()

    types = [e["type"] for e in events]
    assert types == [
        "hello", "job_start", "stage",
        "frame", "frame", "frame", "frame", "frame",
        "stage", "job_done", "answer_start", "answer_done",
    ]
    assert types.index("job_done") < types.index("answer_start")


def test_answer_done_carries_the_fields_the_ui_reads(tmp_path):
    """ui/app.py's `_handle_answer_event` reads `question_id`, `target_id`
    and `score` off `answer_done`; ui/questions.py's `on_answer_done` renders
    `score` verbatim (the content-honesty rule -- see that module's
    docstring). This pins the fixture actually carries them, with the right
    types."""
    sock_path = str(tmp_path / "runner.sock")
    runner = MockRunner(sock_path, load_stream(FIXTURE), speed=100.0)
    runner.start()
    try:
        events = _replay_all(sock_path)
    finally:
        runner.stop()

    answer_done = next(e for e in events if e["type"] == "answer_done")
    assert answer_done["question_id"] == "dhfr_mtx"
    assert answer_done["target_id"] == "dhfr"
    assert isinstance(answer_done["score"], (int, float))


def test_the_answered_targets_job_done_points_at_a_real_loadable_cif():
    """The pocket highlight only fires while the answered target is still
    what a cell is showing (`view.shown_cif_path` must be real) -- so this
    fixture's `job_done.cif_path` has to be more than schema-valid JSON, it
    has to be a real file this repo ships. Checked directly against the raw
    stream (no socket needed) since this is a property of the fixture file,
    not of replay."""
    events = load_stream(FIXTURE)
    job_done = next(e for e in events if e["type"] == "job_done")
    cif_path = pathlib.Path(job_done["cif_path"])
    assert cif_path.is_file(), (
        f"job_done names a cif_path that does not exist: {cif_path}")


def test_the_stream_declares_qa_capable():
    """qa_capable gates the whole rail panel (ui/questions.py's
    `set_qa_capable`) -- a fixture meant to show the panel that never sets
    this would render nothing to look at, which defeats the point of this
    fixture existing."""
    events = load_stream(FIXTURE)
    hello = events[0]
    assert hello["type"] == "hello"
    assert hello["qa_capable"] is True
