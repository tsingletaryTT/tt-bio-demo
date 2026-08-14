"""The wire contract, frozen -- runner half.

The twin of tests/unit/test_protocol_is_frozen.py; see that file's docstring
for why the shared body is duplicated rather than imported. This copy runs
under venv-runner, and adds the one check the UI half cannot make: that no
parent<->worker control line is also a wire type in either direction.
"""

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


def test_no_worker_control_line_is_a_protocol_message_in_either_direction():
    """A control line that is also a wire type is a control line the pool
    will forward to the socket."""
    from runner.workers import CONTROL_FATAL, CONTROL_IDLE, CONTROL_READY
    from protocol.events import CLIENT_MESSAGE_TYPES, EVENT_TYPES
    for kind in (CONTROL_READY, CONTROL_IDLE, CONTROL_FATAL):
        assert kind not in EVENT_TYPES
        assert kind not in CLIENT_MESSAGE_TYPES
