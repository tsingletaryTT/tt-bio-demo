# The thin web viewer

A browser-based alternative to the GTK4 booth app, sitting on the exact same
daemon and the exact same wire protocol (`protocol/events.py`) -- nothing
about the daemon changes to support this. `webview/bridge.py` is, from the
daemon's point of view, just another UI client: it connects to the daemon's
socket the same way `ui/client.py` does, decodes the same events, and
re-publishes them to any number of browser tabs over
[Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events).
A browser's action (currently: picking a target to fold) arrives as a small
JSON `POST` and is re-encoded with the daemon's own `encode_client_message`
before being written to the one real socket connection the bridge owns.

This exists to answer a specific question: could someone watch (and nudge)
the booth from a machine that has no GTK4/Wayland session at all -- over
`ssh`, from a laptop, from a phone on the same network? The architecture
here says yes, with the scope cuts below being the honest cost of "thin."

## Running it with no hardware

`runner/mock.py`'s `MockRunner` is this project's existing hardware-free
test instrument -- it replays a recorded `tests/fixtures/streams/*.jsonl`
fixture over a real Unix socket, to any client that connects, exactly the
shape a real daemon's socket has. Point the bridge at one of those instead
of a real daemon and every event it forwards is real, previously-recorded
protocol traffic -- nothing in this demo path is fabricated for the demo.

Neither `webview.bridge` nor `runner.mock` imports torch or tt-bio, so
either of the project's own venvs works -- `.venvs/venv-ui` is used below,
but `.venvs/venv-runner` would do just as well. Per this project's own
convention, run it through one of those, not a bare `python3`:

```bash
# terminal 1 -- serve a recorded fixture as if it were a live daemon
.venvs/venv-ui/bin/python3 -c "
from runner.mock import MockRunner, load_stream
r = MockRunner('/tmp/tt-bio-demo.sock',
               load_stream('tests/fixtures/streams/with_question.jsonl'),
               speed=1.0)
r.start()
import time; time.sleep(3600)
"

# terminal 2 -- the bridge
.venvs/venv-ui/bin/python3 -m webview.bridge --daemon-socket /tmp/tt-bio-demo.sock --port 8080
```

Then open `http://127.0.0.1:8080/` in a browser. `with_question.jsonl`
exercises the affinity Q&A panel as well as an ordinary fold;
`quad_fold.jsonl` exercises four interleaved chips at once.

Against a **real** daemon, the only difference is `--daemon-socket` pointing
at the daemon's own `--socket` path -- same host, or (with an SSH tunnel or
by binding `--host 0.0.0.0`) a different one. Nothing else changes.

## Why Server-Sent Events, not a WebSocket

The traffic is almost entirely one direction -- daemon events out to
however many tabs are open -- with rare, small actions the other way (a
visitor's tap). SSE covers exactly that shape using nothing beyond the
standard library on the server, and a browser's `EventSource` reconnects on
its own with no client-side code to write. An action is an ordinary `POST`.
A WebSocket would have bought bidirectional framing this traffic pattern
doesn't need, at the cost of either a new dependency or hand-rolling the
handshake/framing/masking -- exactly the kind of thing this project reuses
rather than reinvents elsewhere (`runner/workers.py`'s own rule: "use
tt-bio's own worker machinery, do not reinvent it").

## Where this deliberately stops short of the native app

Built to be honest about scope, not to hide the gap:

- **No ribbon/cartoon reveal.** A fold's live points render as the same
  plain teal point cloud `ui/viewer.py` shows before confidence data
  exists; on `job_done` the browser has no equivalent of `ui/cartoon.py` +
  `ui/secstruct.py` to turn the `.cif` into a colored ribbon, so it just
  holds the last frame, dimmed, with the mean pLDDT as text. Two real paths
  forward, not attempted here: run that pipeline server-side and stream
  precomputed vertex buffers (cheap per viewer if cached per job, not
  recomputed per tab), or run the *actual* `ui/cartoon.py`/`ui/secstruct.py`
  unmodified in the browser via Pyodide (CPython-on-WASM) so there is never
  a second copy of that ~1,250 lines of geometry logic to drift from the
  native app's.
- **The gallery is "targets seen so far," not the real playlist.** It is
  built from `job_start.target_id`s observed live, not
  `playlist/manifest.yaml` (thumbnails, blurbs, `expected_s`) -- reading
  that would mean taking on a YAML dependency this bridge otherwise avoids
  entirely (it is stdlib + numpy only, same discipline as
  `protocol/events.py` itself).
- **A question shows `target_id` and the model's own score, not the
  human-written question text.** `playlist/questions.yaml`'s question
  strings never travel on the wire (by design -- see the affinity Q&A
  spec's protocol section); only the booth's own rail panel has that file
  loaded locally. `answer_done`'s `score` is shown verbatim as "predicted
  probability of binding" per the same content-honesty rule
  `ui/questions.py` follows -- no invented "binds strongly/weakly" tier.
- **The easter egg is not surfaced.** `egg_frame`/`egg_refused` are decoded
  and silently ignored. The native booth keeps it undocumented on purpose
  (`ui/quad.py`: "an easter egg that is documented is a feature"), and a
  labelled web control for it would be exactly that.
- **No reconnect-with-backfill for a tab that was closed and reopened.**
  A newly connected tab is caught up on the last `hello`/`not_ready` only
  (`DaemonLink.last_hello`) -- not the in-progress frame of whatever fold is
  running. It catches the next `frame` normally; it just doesn't see
  partial progress on a fold already underway.
- **`runner.mock.MockRunner`'s reconnect behavior is not the real daemon's.**
  MockRunner replays its whole fixture, from the start, to every connection
  it accepts; the real `EventServer` sends a fresh `hello`/`not_ready` to a
  new connection and then only ever live-broadcasts. If `DaemonLink`'s
  automatic reconnect ever fires against a real daemon, subscribers see a
  new `hello` and nothing else -- against MockRunner (see
  `tests/unit/test_webview_bridge.py`'s
  `test_reconnect_against_mock_runner_replays_the_whole_fixture`) they see
  the whole fixture again. Nothing to fix on the bridge side for this; it's
  a property of the test double, documented so nobody mistakes a
  MockRunner-based reconnect test for proof of real-daemon reconnect
  behavior.

## Tests

`tests/unit/test_webview_bridge.py` -- same venv as
`tests/unit/test_mock_runner.py` (venv-ui; neither `webview.bridge` nor
`runner.mock` imports torch or tt-bio). Run via `scripts/test.sh`, or
directly:

```bash
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_webview_bridge.py -v
```

It uses a real `runner.mock.MockRunner` replaying real fixtures for the
"does the bridge forward what the daemon actually said" tests, and a fake
socket transport (`_FakeSocket`) only for the couple of tests that need to
control exactly what bytes arrive without a MockRunner's own timing
involved.
