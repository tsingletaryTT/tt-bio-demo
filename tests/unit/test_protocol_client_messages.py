"""The client->server direction of the wire protocol.

New in PROTOCOL_VERSION 2. Until this existed the socket was one-way --
runner/server.py broadcast and never read, ui/client.py read and never wrote
-- which is the only reason a visitor's pick could not cause a fold.

These tests live on the UI half (tests/unit/, venv-ui) because protocol/ is
importable from both venvs and this file imports nothing else. The narrower
"both sides agree on the constants" claim is made once per venv, in
tests/unit/test_protocol_is_frozen.py and
tests/unit/runner/test_protocol_is_frozen_runner.py.
"""

import ast
import pathlib
import sys

import pytest

from protocol.events import (
    CLIENT_MESSAGE_TYPES, EVENT_TYPES, MAX_TARGET_ID_LEN, PROTOCOL_VERSION,
    ProtocolError, decode, decode_client_message, encode,
    egg_message, encode_client_message, pick_message,
)


def test_the_version_is_three_because_the_contract_changed_twice():
    """Not decoration: ui/client.py refuses to interpret a daemon whose
    version differs from its own, so this number is the only thing standing
    between a v3 UI and a v2 daemon that will never answer its eggs."""
    assert PROTOCOL_VERSION == 3


def test_a_pick_is_not_an_event():
    """The two directions are separate vocabularies. If a pick is also an
    event, EventServer.broadcast can send one to a UI and ui/client.py will
    hand one to _handle_event as if the daemon had said it."""
    assert "pick" not in EVENT_TYPES
    with pytest.raises(ProtocolError):
        encode(pick_message("trpcage"))
    with pytest.raises(ProtocolError):
        decode(b'{"type":"pick","version":3,"target_id":"trpcage"}\n')


def test_an_event_is_not_a_client_message():
    """The mirror image, and the one that matters for the daemon: a client
    that sends `job_done` must not be able to inject a fold result."""
    assert not (EVENT_TYPES & CLIENT_MESSAGE_TYPES)
    with pytest.raises(ProtocolError):
        encode_client_message({"type": "job_done", "job_id": "j1"})
    with pytest.raises(ProtocolError):
        decode_client_message(
            b'{"type":"job_done","job_id":"j1","cif_path":"/a.cif"}\n')


def test_the_client_vocabulary_is_exactly_two_messages():
    """A general RPC channel is not what this phase is for."""
    assert CLIENT_MESSAGE_TYPES == frozenset({"pick", "egg"})


def test_an_egg_is_not_an_event_either():
    """The same separation the pick has, checked separately rather than
    assumed by symmetry: a client that could put an `egg_frame` on the wire
    could draw whatever it liked into the booth's own viewer."""
    assert "egg" not in EVENT_TYPES
    with pytest.raises(ProtocolError):
        encode(egg_message("abc123"))
    with pytest.raises(ProtocolError):
        encode_client_message({"type": "egg_frame", "egg_id": "abc123"})


def test_an_egg_carries_the_version_and_round_trips():
    message = egg_message("abc123")
    assert message["type"] == "egg"
    assert message["version"] == PROTOCOL_VERSION
    assert message["egg_id"] == "abc123"
    assert decode_client_message(encode_client_message(message)) == message


@pytest.mark.parametrize("bad", [
    b'{"type":"egg","version":%d}\n' % PROTOCOL_VERSION,
    b'{"type":"egg","version":%d,"egg_id":""}\n' % PROTOCOL_VERSION,
    b'{"type":"egg","version":%d,"egg_id":17}\n' % PROTOCOL_VERSION,
    b'{"type":"egg","version":%d,"egg_id":"%s"}\n'
    % (PROTOCOL_VERSION, b"x" * (MAX_TARGET_ID_LEN + 1)),
])
def test_an_egg_id_is_validated_exactly_as_a_target_id_is(bad):
    """The daemon reads this off a socket a booth exposes to a room full of
    strangers' laptops. The `egg` message got its own field name and must not
    have got its own (absent) validation with it -- which is precisely what
    happens if `CLIENT_MESSAGE_FIELDS` and `CLIENT_MESSAGE_TYPES` drift."""
    with pytest.raises(ProtocolError):
        decode_client_message(bad)


def test_a_pick_carries_the_version_it_was_written_against():
    message = pick_message("trpcage")
    assert message["type"] == "pick"
    assert message["version"] == PROTOCOL_VERSION
    assert message["target_id"] == "trpcage"


def test_a_pick_round_trips():
    assert decode_client_message(
        encode_client_message(pick_message("trpcage"))) == pick_message("trpcage")


def test_an_encoded_pick_is_exactly_one_line():
    """The daemon frames on newlines. A target_id containing one must not be
    able to split a message into two."""
    line = encode_client_message(pick_message("trp\ncage"))
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1


def test_a_message_from_a_different_protocol_version_is_refused():
    with pytest.raises(ProtocolError):
        decode_client_message(
            b'{"type":"pick","version":1,"target_id":"trpcage"}\n')


def test_a_message_with_no_version_is_refused():
    """An unversioned message is one we cannot reason about at all."""
    with pytest.raises(ProtocolError):
        decode_client_message(b'{"type":"pick","target_id":"trpcage"}\n')


def test_malformed_json_is_a_ProtocolError_not_a_crash():
    for junk in (b"not json{\n", b"\n", b'{"type":\n', b"\xff\xfe\n"):
        with pytest.raises(ProtocolError):
            decode_client_message(junk)


def test_a_json_array_is_refused():
    with pytest.raises(ProtocolError):
        decode_client_message(b'["pick","trpcage"]\n')


def test_an_unknown_message_type_is_refused():
    with pytest.raises(ProtocolError):
        decode_client_message(
            b'{"type":"shutdown","version":2,"target_id":"x"}\n')


def test_an_absurd_target_id_is_refused():
    """The daemon reads this off a socket. A megabyte target_id is a
    megabyte the daemon should never have allocated, and the length limit
    is the only thing that says so."""
    huge = "a" * (MAX_TARGET_ID_LEN + 1)
    with pytest.raises(ProtocolError):
        decode_client_message(encode_client_message(
            {"type": "pick", "version": PROTOCOL_VERSION, "target_id": huge}))


def test_a_target_id_at_the_limit_is_accepted():
    """A limit that is off by one is a limit that rejects real targets."""
    ok = "a" * MAX_TARGET_ID_LEN
    assert decode_client_message(encode_client_message(
        {"type": "pick", "version": PROTOCOL_VERSION,
         "target_id": ok}))["target_id"] == ok


def test_a_non_string_target_id_is_refused():
    for bad in (17, None, ["trpcage"], {"a": 1}):
        with pytest.raises(ProtocolError):
            decode_client_message(
                encode_client_message({"type": "pick",
                                       "version": PROTOCOL_VERSION,
                                       "target_id": bad}))


def test_an_empty_target_id_is_refused():
    with pytest.raises(ProtocolError):
        decode_client_message(
            b'{"type":"pick","version":2,"target_id":""}\n')


def test_this_module_still_imports_nothing_but_stdlib_and_numpy():
    """The rule that makes protocol/ importable from BOTH venvs, enforced
    against the file rather than against anyone's memory of it. The client
    direction is exactly the kind of addition that reaches for a validation
    library."""
    source = pathlib.Path("protocol/events.py").read_text()
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names) | {"numpy"}, sorted(roots)
