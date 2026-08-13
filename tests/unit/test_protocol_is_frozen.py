"""The wire contract, frozen -- UI half.

The body of this file is duplicated, deliberately and near-verbatim, in
tests/unit/runner/test_protocol_is_frozen_runner.py. Both halves make the
same claim about the same three constants, and each must hold it in the
interpreter that actually runs it: scripts/test.sh runs this file under
venv-ui and its twin under venv-runner, so a bump made in one checkout and
not the other is caught here rather than at a booth.

The two files are not identical: this one additionally ratchets the
committed replay fixtures against the constant (a check the runner half
cannot usefully make, since nothing on that side replays them), and the
runner copy additionally checks the worker control vocabulary (a check this
side cannot make at all, because the UI may not import runner.*).
"""

import json
import pathlib

from protocol.events import CLIENT_MESSAGE_TYPES, EVENT_TYPES, PROTOCOL_VERSION


def test_the_protocol_version_is_two_on_this_side_too():
    """Both halves must agree on this number or the UI refuses the daemon at
    `hello`. A bump made in one venv's checkout and not the other is exactly
    the failure this pair of files exists to catch."""
    assert PROTOCOL_VERSION == 2


def test_the_event_vocabulary_is_unchanged_by_multi_chip():
    """Multi-chip is a scheduling change. If this fails, something leaked
    onto the wire that should not have -- a worker control line is the
    likely candidate."""
    assert EVENT_TYPES == frozenset(
        {"hello", "not_ready", "job_start", "stage", "frame",
         "job_done", "job_error", "card_state"})


def test_the_client_vocabulary_is_exactly_one_message():
    """The pick, and nothing else. A second client->server message is a
    third version, a decision, and a task of its own."""
    assert CLIENT_MESSAGE_TYPES == frozenset({"pick"})


def test_the_two_directions_never_overlap():
    assert not (EVENT_TYPES & CLIENT_MESSAGE_TYPES)


def test_no_committed_fixture_advertises_a_stale_protocol_version():
    """A fixture whose `hello` says v1 makes ui/client.py declare the stream
    incompatible and stop reading -- the failure looks like "the UI tests
    hang and see no events", which is a long way from "somebody bumped a
    constant". Every .jsonl under tests/fixtures/streams/ is replayed to a
    real EventClient by something, so every one of them has to move."""
    from protocol.events import PROTOCOL_VERSION
    for path in sorted(pathlib.Path("tests/fixtures/streams").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            if event.get("type") == "hello":
                assert event["version"] == PROTOCOL_VERSION, path
