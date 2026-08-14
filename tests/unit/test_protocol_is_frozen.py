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


def test_the_protocol_version_is_three_on_this_side_too():
    """Both halves must agree on this number or the UI refuses the daemon at
    `hello`. A bump made in one venv's checkout and not the other is exactly
    the failure this pair of files exists to catch.

    2 -> 3 when the easter egg moved onto the chips: one new client message
    and two new events, both directions changed, so both halves move."""
    assert PROTOCOL_VERSION == 3


def test_the_event_vocabulary_is_exactly_these_ten():
    """Multi-chip added nothing here -- scheduling across four cards is not a
    wire change. The easter egg added exactly two, and they are named here so
    that a THIRD arriving is a decision somebody made on purpose. If this
    fails for anything else, something leaked onto the wire that should not
    have -- a worker control line is the likely candidate."""
    assert EVENT_TYPES == frozenset(
        {"hello", "not_ready", "job_start", "stage", "frame",
         "job_done", "job_error", "card_state",
         "egg_frame", "egg_refused"})


def test_an_egg_frame_is_not_a_fold_frame():
    """The separation the UI's routing depends on. A `frame` is looked up by
    `job_id` and drawn into whichever fold slot owns it; an egg has no job, so
    if these two ever became one type an easter egg would be able to land a
    logo in the middle of a visitor's protein."""
    assert "egg_frame" in EVENT_TYPES and "frame" in EVENT_TYPES
    assert "egg_frame" != "frame"


def test_the_client_vocabulary_is_exactly_two_messages():
    """The pick and the egg, and nothing else. Each addition here is a
    version bump and a decision; the previous revision of this test said "a
    second client->server message is a third version, a decision, and a task
    of its own", and that is exactly what the egg was."""
    assert CLIENT_MESSAGE_TYPES == frozenset({"pick", "egg"})


def test_every_client_message_has_a_validation_rule():
    """`decode_client_message` looks a message's one id field up in this
    table and refuses a type that is not in it -- so a message added to the
    vocabulary and not to the table is refused rather than accepted with
    nothing checked. This pins the two together."""
    from protocol.events import CLIENT_MESSAGE_FIELDS
    assert set(CLIENT_MESSAGE_FIELDS) == set(CLIENT_MESSAGE_TYPES)


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
