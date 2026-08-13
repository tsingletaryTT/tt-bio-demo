import base64
import numpy as np
import pytest

from protocol.events import (
    EVENT_TYPES,
    PROTOCOL_VERSION,
    STAGE_BANDS,
    STAGE_ORDER,
    ProtocolError,
    decode,
    encode,
    pack_coords,
    unpack_coords,
    within_stage_frac,
)


def test_protocol_version_is_the_one_this_build_speaks():
    # Was `== 1` until the socket gained its client->server direction. The
    # authoritative pin now lives in tests/unit/test_protocol_is_frozen.py
    # and its runner-half twin, which assert the number in BOTH venvs -- a
    # bump applied to one checkout and not the other is the failure that
    # actually happens, and this file only ever runs under venv-ui.
    assert PROTOCOL_VERSION == 2


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


# ---------------------------------------------------------------------------
# STAGE_ORDER / STAGE_BANDS / within_stage_frac.
#
# These moved here from runner/shaping.py + runner/folder.py (see
# protocol/events.py's own comment on STAGE_ORDER): the UI venv cannot
# import runner.*, so this is the one place both venvs can test the shared
# stage contract at all. runner/shaping.py re-exports STAGE_ORDER (a bare
# import) and tests/unit/runner/test_shaping.py separately pins that
# re-export keeps working; this file pins the actual source of truth.
# ---------------------------------------------------------------------------

def test_stage_order_is_the_six_stage_protocol_vocabulary():
    assert STAGE_ORDER == ("msa", "prep", "trunk", "diffusion", "confidence", "saving")


def test_stage_bands_cover_every_stage_contiguously_from_zero_to_one():
    assert tuple(STAGE_BANDS) == STAGE_ORDER
    bands = [STAGE_BANDS[stage] for stage in STAGE_ORDER]
    assert bands[0][0] == 0.0
    assert bands[-1][1] == 1.0
    for (_start, end), (next_start, _next_end) in zip(bands, bands[1:]):
        assert end == next_start, "bands must be contiguous -- no gap, no overlap"
    for start, end in bands:
        assert start < end


def test_within_stage_frac_maps_a_bands_own_edges_to_zero_and_one():
    start, end = STAGE_BANDS["diffusion"]
    assert within_stage_frac("diffusion", start) == pytest.approx(0.0)
    assert within_stage_frac("diffusion", end) == pytest.approx(1.0)


def test_within_stage_frac_converts_the_briefs_own_worked_example():
    # diffusion's band is (0.15, 0.95): a wire value of 0.55 is
    # (0.55 - 0.15) / (0.95 - 0.15) = 0.4 / 0.8 = 0.5 of the way through
    # diffusion itself.
    assert within_stage_frac("diffusion", 0.55) == pytest.approx(0.5)


def test_within_stage_frac_is_not_the_identity_function():
    """A wire value inside a stage's band must not pass straight through --
    that would defeat the entire point of the conversion (see the brief's
    own warning: 'a bar sits at 15% through the whole of diffusion')."""
    assert within_stage_frac("diffusion", 0.55) != pytest.approx(0.55)


@pytest.mark.parametrize("stage", ["msa", "prep", "trunk", "confidence", "saving"])
def test_within_stage_frac_works_for_every_stage_not_just_diffusion(stage):
    start, end = STAGE_BANDS[stage]
    midpoint = (start + end) / 2.0
    assert within_stage_frac(stage, midpoint) == pytest.approx(0.5)


def test_within_stage_frac_clamps_out_of_range_wire_values():
    start, end = STAGE_BANDS["trunk"]
    assert within_stage_frac("trunk", start - 1.0) == 0.0
    assert within_stage_frac("trunk", end + 1.0) == 1.0


def test_within_stage_frac_on_an_unknown_stage_does_not_raise():
    """A future protocol stage must not break the conversion either --
    symmetric with stage_rows' own unknown-stage contract."""
    assert within_stage_frac("something-new", 0.5) == pytest.approx(0.5)
    assert within_stage_frac("something-new", 5.0) == 1.0
    assert within_stage_frac("something-new", -5.0) == 0.0
