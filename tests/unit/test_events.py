import base64
import numpy as np
import pytest

from protocol.events import (
    EVENT_TYPES,
    PROTOCOL_VERSION,
    ProtocolError,
    decode,
    encode,
    pack_coords,
    unpack_coords,
)


def test_protocol_version_is_one():
    assert PROTOCOL_VERSION == 1


def test_all_spec_event_types_present():
    assert EVENT_TYPES == frozenset(
        {"hello", "not_ready", "job_start", "stage", "frame",
         "job_done", "job_error", "card_state"}
    )


def test_encode_appends_newline_and_decodes_back():
    event = {"type": "stage", "job_id": "j1", "stage": "trunk", "frac": 0.3}
    line = encode(event)
    assert line.endswith(b"\n")
    assert decode(line) == event


def test_encode_rejects_unknown_type():
    with pytest.raises(ProtocolError, match="unknown event type"):
        encode({"type": "nonsense"})


def test_decode_rejects_missing_type():
    with pytest.raises(ProtocolError, match="missing 'type'"):
        decode(b'{"job_id": "j1"}\n')


def test_decode_rejects_malformed_json():
    with pytest.raises(ProtocolError, match="malformed JSON"):
        decode(b'{"type": "stage"\n')


def test_decode_rejects_non_object():
    with pytest.raises(ProtocolError, match="not a JSON object"):
        decode(b'[1, 2, 3]\n')


def test_coords_round_trip_preserves_values_and_shape():
    coords = np.array([[1.5, -2.25, 3.0], [0.0, 0.5, -1.0]], dtype=np.float64)
    restored = unpack_coords(pack_coords(coords))
    assert restored.shape == (2, 3)
    assert restored.dtype == np.float32
    np.testing.assert_allclose(restored, coords, rtol=0, atol=1e-6)


def test_pack_coords_is_base64_of_float32_little_endian():
    coords = np.array([[1.0, 2.0, 3.0]], dtype=np.float32)
    raw = base64.b64decode(pack_coords(coords))
    assert raw == np.array([1.0, 2.0, 3.0], dtype="<f4").tobytes()


def test_pack_coords_rejects_wrong_shape():
    with pytest.raises(ProtocolError, match="shape"):
        pack_coords(np.zeros((4,), dtype=np.float32))


def test_unpack_coords_rejects_truncated_buffer():
    truncated = base64.b64encode(b"\x00" * 10).decode("ascii")
    with pytest.raises(ProtocolError, match="not a whole number"):
        unpack_coords(truncated)
