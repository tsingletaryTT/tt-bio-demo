from protocol.events import (EVENT_TYPES, PROTOCOL_VERSION, decode, encode,
                              decode_client_message, question_message)


def test_protocol_version_bumped_for_questions():
    assert PROTOCOL_VERSION == 4


def test_answer_events_are_recognized():
    for etype in ("answer_start", "answer_done", "answer_error"):
        assert etype in EVENT_TYPES


def test_question_message_round_trips():
    msg = question_message("q1", "dhfr")
    assert msg == {"type": "question", "version": PROTOCOL_VERSION,
                   "question_id": "q1", "target_id": "dhfr"}
    from protocol.events import encode_client_message
    decoded = decode_client_message(encode_client_message(msg))
    assert decoded == msg


def test_answer_done_encodes_and_decodes():
    event = {"type": "answer_done", "question_id": "q1", "target_id": "dhfr",
             "score": 0.42}
    assert decode(encode(event)) == event


def test_answer_error_message_is_present_but_not_special():
    # Same rule as job_error: the runner may put detail in `message`; it is
    # this event's job only to carry it, never to sanitize it -- sanitizing
    # is the UI's job at display time, not the protocol's.
    event = {"type": "answer_error", "question_id": "q1", "target_id": "dhfr",
             "message": "nesso1 raised ValueError: ..."}
    assert decode(encode(event)) == event
