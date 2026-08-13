"""The wire contract, frozen -- runner half.

The twin of tests/unit/test_protocol_is_frozen.py; see that file's docstring
for why the shared body is duplicated rather than imported. This copy runs
under venv-runner, and adds the one check the UI half cannot make: that no
parent<->worker control line is also a wire type in either direction.
"""

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


def test_no_worker_control_line_is_a_protocol_message_in_either_direction():
    """A control line that is also a wire type is a control line the pool
    will forward to the socket."""
    from runner.workers import CONTROL_FATAL, CONTROL_IDLE, CONTROL_READY
    from protocol.events import CLIENT_MESSAGE_TYPES, EVENT_TYPES
    for kind in (CONTROL_READY, CONTROL_IDLE, CONTROL_FATAL):
        assert kind not in EVENT_TYPES
        assert kind not in CLIENT_MESSAGE_TYPES
