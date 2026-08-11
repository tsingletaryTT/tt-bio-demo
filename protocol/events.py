"""Wire protocol shared by the runner and the UI.

Imports must stay limited to the standard library and numpy: this module is
imported both by the UI on system python3 and by the runner inside the tt-bio
venv, and any richer dependency would have to exist in both.
"""

import base64
import json

import numpy as np

PROTOCOL_VERSION = 1

EVENT_TYPES = frozenset(
    {"hello", "not_ready", "job_start", "stage", "frame",
     "job_done", "job_error", "card_state"}
)


class ProtocolError(Exception):
    """A message could not be encoded or decoded."""


def encode(event):
    """Serialize one event to a newline-terminated JSON line."""
    kind = event.get("type")
    if kind not in EVENT_TYPES:
        raise ProtocolError(f"unknown event type: {kind!r}")
    return (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line):
    """Parse one newline-terminated JSON line into an event dict."""
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"malformed JSON: {exc}") from exc
    if not isinstance(event, dict):
        raise ProtocolError("not a JSON object")
    if "type" not in event:
        raise ProtocolError("missing 'type'")
    if event["type"] not in EVENT_TYPES:
        raise ProtocolError(f"unknown event type: {event['type']!r}")
    return event


def pack_coords(a):
    """Pack an (N, 3) coordinate array as base64 of little-endian float32."""
    arr = np.asarray(a)
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise ProtocolError(f"coordinates must have shape (N, 3), got {arr.shape}")
    buf = np.ascontiguousarray(arr, dtype="<f4").tobytes()
    return base64.b64encode(buf).decode("ascii")


def unpack_coords(s):
    """Inverse of pack_coords. Returns an (N, 3) float32 array."""
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception as exc:
        raise ProtocolError(f"invalid base64: {exc}") from exc
    if len(raw) % 12 != 0:
        raise ProtocolError(
            f"buffer of {len(raw)} bytes is not a whole number of 3-vectors"
        )
    return np.frombuffer(raw, dtype="<f4").reshape(-1, 3)
