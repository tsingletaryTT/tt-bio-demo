"""Socket client for the runner's event stream.

Deliberately free of GTK imports so it can be tested headlessly. `on_event`
fires on a background thread; GTK callers must marshal to the main loop
themselves (see ui/app.py, which uses GLib.idle_add).

Since protocol v2 the socket is two-way, so this module also owns the UI's
send direction: `send()` / `send_pick()`, a bounded outbox, and one sender
thread. Everything about that half is shaped by where it is called from -- a
GTK callback, on the main loop -- and by two things that are fatal there:

  * An exception raised inside a GLib callback freezes that source for the
    life of the process. The booth would stop answering the daemon entirely,
    with nothing on screen to say why. So `send()` returns False; it does not
    raise, ever, for any input.
  * A `sendall` issued on the main loop blocks for as long as the peer's
    receive buffer stays full, and the daemon is a process that can be
    stopped, wedged, or in the middle of four folds. A blocked main loop is a
    frozen booth with a live-looking screen. So `send()` queues and a
    background thread does the writing; `send()` never touches the socket.
"""

import collections
import itertools
import logging
import socket
import threading

from protocol.events import (PROTOCOL_VERSION, ProtocolError, decode,
                             egg_message, encode_client_message, pick_message)

log = logging.getLogger(__name__)

# How many client->server messages may wait to be written before the oldest is
# dropped.
#
# Small on purpose, and the small size is the design rather than a saving. A
# pick means "fold this now": it is worth sending in the next few
# milliseconds and worth nothing at all a minute later, to a visitor who has
# already walked away from the screen. The outbox exists to keep the GTK main
# loop off the socket -- one message in flight is its normal depth -- not to
# accumulate a history of taps for a daemon that is not listening. Eight is
# more than a hand can generate between two writes (each is one ~50-byte
# syscall) and few enough that a stalled daemon can never leave a queue of
# stale intentions to deliver on reconnect. Anything queued while there is no
# connection is dropped outright when one is established; see `_open_outbox`.
OUTBOX_MAX = 8

# Names the threads uniquely so two clients in one process (the tests, and a
# future UI that watches two daemons) are told apart in a log line or a stack
# dump.
_client_seq = itertools.count(1)


class LatestFrame:
    """A one-slot buffer that keeps only the newest frame.

    Diffusion frames are advisory: if the renderer falls behind, showing the
    most recent coordinates is strictly better than working through a backlog,
    which would make the animation lag further behind with every frame.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self.dropped = 0

    def put(self, event):
        with self._lock:
            if self._frame is not None:
                self.dropped += 1
            self._frame = event

    def take(self):
        with self._lock:
            frame, self._frame = self._frame, None
            return frame


class LatestFrameByJob:
    """`LatestFrame`'s contract, once per `job_id`.

    Four chips fold at once, so there are up to four live frame streams and
    one shared slot is exactly wrong for them: whichever fold happened to be
    fastest would overwrite the other three every drain, and every cell on
    screen would show that one fold's coordinates. Latest-wins is still the
    right rule -- a diffusion frame is advisory, and the newest coordinates
    for a fold beat a backlog of its older ones -- so it is applied PER
    FOLD rather than abandoned.

    It lives beside `LatestFrame` because this is where that argument (and
    the one-slot buffer it produced) already lives, and because a second
    module holding half of it is a second place for the two halves to drift.

    Bounded, deliberately. An all-day booth folds thousands of jobs and a
    dict that remembered every one is a leak with a screen attached; a job
    that stopped producing frames an hour ago has nothing anyone wants to
    draw. Past `max_jobs`, the OLDEST job's slot is evicted -- oldest by
    when it last produced a frame, which is the only ordering this class can
    honestly know.

    Thread-safe the same way `LatestFrame` is: `put` runs on the socket
    reader thread, `take_all` on the GTK main loop.
    """

    def __init__(self, max_jobs=8):
        self._lock = threading.Lock()
        # job_id -> newest event for that job, least-recently-written first.
        self._frames = collections.OrderedDict()
        self.max_jobs = max(1, int(max_jobs))
        # TWO COUNTERS, BECAUSE THEY MEAN OPPOSITE THINGS.
        #
        # `dropped` counts a frame superseded by a newer one for the SAME
        # job before the renderer drew it. At 30 Hz against a 33 ms drain
        # that is the ordinary case -- it is what a latest-wins buffer is
        # for -- so a large number here is normal and says nothing is wrong.
        #
        # `evicted` counts a whole JOB pushed out because more jobs had
        # frames waiting than this buffer has slots. That is not normal: it
        # means a cell's frames were thrown away wholesale, and it is the
        # signal worth reading (docs/followups.md asks for the cheapest
        # available "the renderer is falling behind" indicator; conflating
        # the two, as this did, is why the number could not be surfaced --
        # it was always huge and always meaningless).
        self.dropped = 0
        self.evicted = 0

    def __len__(self):
        """How many jobs currently have a frame waiting.

        Public because the bound is a BEHAVIOUR: a test that reached into
        `_frames` would be testing something adjacent to it (this project's
        recurring test defect -- docs/followups.md).
        """
        with self._lock:
            return len(self._frames)

    def put(self, event):
        """Keep this frame as its job's newest, evicting an old job if need be.

        A frame with no `job_id` gets its own slot under `None`. It cannot be
        routed to a cell (`SlotRouter` binds job ids to cells, and only
        `job_start` carries a `card`), so ui/app.py drops it at drain time --
        but dropping it HERE would mean the buffer silently disagreed with
        the caller about what "latest wins" applies to.
        """
        job_id = event.get("job_id") if hasattr(event, "get") else None
        with self._lock:
            if job_id in self._frames:
                # Superseded before it was ever drawn -- the ordinary case at
                # 30Hz, counted for the same reason `LatestFrame` counts it.
                self.dropped += 1
                del self._frames[job_id]
            self._frames[job_id] = event
            while len(self._frames) > self.max_jobs:
                self._frames.popitem(last=False)
                self.evicted += 1

    def take_all(self):
        """Every job's newest frame, as `{job_id: event}`, emptying the buffer.

        All of them in one call rather than one job at a time: the caller
        draws four cells from one drain, and asking per job would make the
        set of jobs it has to ask about a second thing to keep right.
        A caller that decides not to draw one of them (a cell holding a
        finished structure) puts that frame back -- see ui/app.py's
        `_drain_frames`, where suppressed-not-discarded is the rule that lets
        a cell cut straight to live diffusion when its dwell expires.
        """
        with self._lock:
            frames, self._frames = dict(self._frames), collections.OrderedDict()
            return frames


# THE ONE NUMBER THE TWO JOIN TIMEOUTS BELOW ARE DERIVED FROM.
#
# A worker thread blocked in a socket call cannot notice `_stop` until that
# call returns, and it returns when this timeout fires. So every join in
# `stop()` has to outlast it, or `stop()` returns while a thread is still
# inside a read or a write -- a real teardown guarantee turning into a
# hopeful one.
#
# That relationship used to be PROSE. Both joins carried a comment saying
# "longer than the socket timeout set in _session (5.0s)", with the 5.0 and
# the 6.0 written out separately at three call sites, so an isolated edit to
# any one of them silently reintroduced the bug the pairing was written to
# fix (docs/followups.md). Derived here instead, so the relationship cannot
# be edited apart.
SOCKET_TIMEOUT_S = 5.0

# The reader: blocked in `for line in stream`, released one socket timeout
# after `_stop` at the latest.
READER_JOIN_TIMEOUT_S = SOCKET_TIMEOUT_S + 1.0

# The sender: blocked in `sendall` to a daemon that has stopped reading. It
# gets more headroom because it is joined FIRST and a partial write can take
# a second timeout to unwind.
SENDER_JOIN_TIMEOUT_S = SOCKET_TIMEOUT_S + 3.0


class EventClient:
    """Connects to the runner, decodes events, reconnects when dropped.

    Two threads, one socket, and they share nothing but the connection
    reference: a reader (`_run` / `_session`), which owns the connection's
    whole lifecycle including every reconnect, and a sender (`_send_loop`),
    which only ever writes to whatever connection the reader has published.
    Neither waits on the other -- a daemon that has stopped reading must not
    stall the fold events arriving on screen, and a daemon that has gone quiet
    must not hold up a visitor's pick.
    """

    def __init__(self, socket_path, on_event, on_state_change=None,
                 reconnect_delay=1.0, outbox_max=OUTBOX_MAX):
        self.socket_path = socket_path
        self.on_event = on_event
        self.on_state_change = on_state_change
        self.reconnect_delay = reconnect_delay
        self.state = "disconnected"
        self._stop = threading.Event()
        self._thread = None
        self._sender_thread = None
        self._name = f"EventClient-{next(_client_seq)}"

        # --- the send direction -------------------------------------------
        # Public, like SlotRouter.tracked_jobs, because a test asserting the
        # outbox is bounded must be able to see the bound without reaching
        # into a private field -- an "adjacent to the behaviour" assertion is
        # this project's recurring test defect (docs/followups.md).
        self.dropped_sends = 0
        self._outbox_max = max(1, int(outbox_max))
        self._outbox = collections.deque()
        # Guards `_outbox` and `dropped_sends`, and wakes the sender. Held for
        # dict-sized moments only: never across a write, never while
        # `_conn_lock` is held (see `_send_loop` for the lock order).
        self._outbox_cond = threading.Condition()
        # The socket `_session` is currently reading from, or None. Published
        # here so the sender thread can write to it without opening, closing
        # or reconnecting anything of its own -- `_run` owns recovery and must
        # stay the only thing that does, or two threads race to reconnect and
        # the UI ends up with two sessions it thinks are one.
        self._conn = None
        # Guards the `_conn` REFERENCE, and nothing else. Deliberately not
        # held across `sendall` (the same ruling as EventServer._send_lock vs
        # _lock): a daemon that has stopped draining blocks a write for the
        # socket's full timeout, and if that were also the lock `_session`
        # needs to retire a dead connection, a stalled daemon would stall the
        # read direction too -- which is the exact stall
        # test_the_read_direction_still_works_while_sending exists to catch.
        self._conn_lock = threading.Lock()

    @property
    def pending_sends(self):
        """How many client messages are waiting to be written."""
        with self._outbox_cond:
            return len(self._outbox)

    def send(self, message):
        """Queue one client->server message. Returns whether it was queued.

        Never raises and never blocks: this is called from a GLib callback
        (see the module docstring). A message that cannot be encoded, a
        daemon whose protocol version this build has refused to interpret, and
        a client that has already been stopped all come back as False.

        Returning True means "accepted for sending", not "delivered". There is
        no delivery receipt in this protocol and a pick is not worth inventing
        one for: the daemon may still be gone by the time the sender reaches
        it, in which case the message is dropped and counted in
        `dropped_sends`.
        """
        # Once the versions disagree the UI has declared it cannot interpret
        # this daemon (see protocol/events.py's PROTOCOL_VERSION note, and
        # _session below, which sets this and never retries). Talking to it
        # anyway is the one thing worse than staying quiet: the booth would be
        # promising a capability -- "your tap folds this" -- that the daemon
        # on the other end has no idea how to honour.
        if self.state == "incompatible":
            log.debug("not sending to a daemon this build cannot interpret")
            return False
        try:
            line = encode_client_message(message)
        except ProtocolError as exc:
            log.warning("refusing to send malformed client message: %s", exc)
            return False
        except Exception:
            # Belt and braces around a GLib callback: encode_client_message
            # promises ProtocolError, but a bug there (or a __repr__ raising
            # inside json.dumps) must still not reach the main loop.
            log.exception("refusing to send client message: unexpected error")
            return False
        with self._outbox_cond:
            if self._stop.is_set():
                return False
            while len(self._outbox) >= self._outbox_max:
                # Oldest out, not newest refused. When the daemon has stalled,
                # the picks worth keeping are the ones the visitor just made.
                self._outbox.popleft()
                self.dropped_sends += 1
            self._outbox.append(line)
            self._outbox_cond.notify()
        return True

    def send_pick(self, target_id):
        """Ask the daemon to fold `target_id` now. Returns whether it queued."""
        return self.send(pick_message(target_id))

    def send_egg(self, egg_id):
        """Ask the daemon to run the easter egg on a chip.

        Returns False -- promptly, and without raising -- when there is no
        daemon, when this build has refused the daemon's protocol version, or
        when the message cannot be encoded. The caller (ui/app.py) treats
        every one of those as "no chip is going to answer" and starts the CPU
        descent with the CPU label, which is the honest half of this feature.
        """
        return self.send(egg_message(egg_id))

    def start(self):
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"{self._name}-reader")
        self._sender_thread = threading.Thread(
            target=self._send_loop, daemon=True, name=f"{self._name}-sender")
        self._thread.start()
        self._sender_thread.start()

    def stop(self):
        self._stop.set()
        # Wake a sender parked on an empty outbox (or on a connection that
        # never arrived) instead of letting it sit out its poll interval.
        with self._outbox_cond:
            self._outbox_cond.notify_all()
        if self._sender_thread is not None:
            # SENDER_JOIN_TIMEOUT_S is derived from SOCKET_TIMEOUT_S, for
            # the same reason the reader's join is: a sender blocked in
            # `sendall` to a daemon that has stopped reading cannot notice
            # `_stop` until that write returns, and it returns when the socket
            # timeout fires. A shorter join here would let stop() return while
            # the thread was still inside a write -- which is precisely what
            # test_stop_returns_only_after_a_parked_sender_thread_has_exited
            # arranges and checks.
            #
            # Joined BEFORE the reader, and that order is load-bearing: the
            # sender shuts the connection down as it exits (_wake_the_reader),
            # which is what lets the reader's join below return at once
            # instead of sitting out the socket's read timeout.
            self._sender_thread.join(timeout=SENDER_JOIN_TIMEOUT_S)
        if self._thread is not None:
            # READER_JOIN_TIMEOUT_S exceeds the socket read timeout by
            # construction: when the thread is blocked in `for line in
            # stream` with no data pending, it cannot notice `_stop` until
            # that read call returns -- either because data arrives or
            # because the socket timeout fires. A shorter join timeout here
            # would let stop() return before the thread has actually
            # exited, which is a benign no-op in tests (where the mock
            # runner's own .stop() closes the connection and unblocks the
            # read immediately) but not a real guarantee.
            self._thread.join(timeout=READER_JOIN_TIMEOUT_S)

    def _set_state(self, state):
        if state != self.state:
            self.state = state
            if self.on_state_change is not None:
                self.on_state_change(state)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._session()
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                log.debug("runner unavailable: %s", exc)
            # Check for "incompatible" before touching state: it was just set
            # inside _session(), and calling _set_state("disconnected")
            # unconditionally here would immediately clobber it, defeating
            # the no-retry guard below and causing an infinite reconnect
            # spam against a runner that speaks the wrong protocol version.
            if self.state == "incompatible":
                return
            self._set_state("disconnected")
            self._stop.wait(self.reconnect_delay)

    def _session(self):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(SOCKET_TIMEOUT_S)
        conn.connect(self.socket_path)
        with conn:
            self._open_outbox(conn)
            try:
                # After _open_outbox, never before: once a caller can see
                # "connected" it must be true that its next pick goes to this
                # connection and is not swept away as a leftover of the
                # outage that just ended.
                self._set_state("connected")
                self._read_events(conn)
            finally:
                self._close_outbox(conn)

    def _read_events(self, conn):
        with conn.makefile("rb") as stream:
            for line in stream:
                if self._stop.is_set():
                    return
                try:
                    event = decode(line)
                except ProtocolError as exc:
                    log.warning("dropping undecodable line: %s", exc)
                    continue
                if event["type"] == "hello":
                    if event.get("version") != PROTOCOL_VERSION:
                        log.error(
                            "runner speaks protocol v%s, UI speaks v%s; "
                            "refusing to interpret its messages",
                            event.get("version"), PROTOCOL_VERSION,
                        )
                        self._set_state("incompatible")
                        return
                try:
                    self.on_event(event)
                except Exception:
                    # A raising callback (e.g. a bug in a later GTK
                    # marshaling wrapper) must not be mistaken for a
                    # connection failure and must not cost the rest of the
                    # stream: one bad event should neither end the
                    # reconnect loop nor leave `state` wedged at
                    # "connected" with no further callbacks. Catch
                    # Exception, not BaseException, so KeyboardInterrupt/
                    # SystemExit still propagate and can stop the thread.
                    log.exception("on_event callback raised; continuing")

    # --- the send direction ------------------------------------------------

    def _open_outbox(self, conn):
        """Publish `conn` to the sender, discarding what was queued without it.

        The discard is the freshness rule, and it is not the same rule as
        OUTBOX_MAX. A visitor taps, the daemon is down, they walk away; thirty
        seconds later the daemon comes back and the booth starts folding
        something nobody is watching, with the pipeline panel animating for an
        empty chair. A pick is an instruction about *now*, so anything that
        was queued while there was nowhere to send it is stale by definition
        and is counted as dropped, not delivered late.
        """
        with self._outbox_cond:
            stale = len(self._outbox)
            if stale:
                log.debug("dropping %d pick(s) queued while disconnected", stale)
                self._outbox.clear()
                self.dropped_sends += stale
        with self._conn_lock:
            self._conn = conn
        with self._outbox_cond:
            self._outbox_cond.notify()

    def _close_outbox(self, conn):
        """Retire `conn`, so the sender parks instead of writing to a corpse.

        Guarded on identity rather than blindly clearing: `_run` has already
        started the next session by the time a slow caller gets here in some
        interleavings, and clearing then would silently disable the send
        direction for a connection that is perfectly alive.
        """
        with self._conn_lock:
            if self._conn is conn:
                self._conn = None

    def _send_loop(self):
        """Write queued client messages to whatever connection is current.

        Deliberately does NOT reconnect, and deliberately does not create,
        close or retry a socket: `_run` owns the connection lifecycle and
        stays the only thing that does. Everything this thread can do about a
        failure is drop the message and go round again -- which is the right
        answer anyway, since by the time a pick could be retried it is no
        longer the pick the visitor made.
        """
        try:
            while not self._stop.is_set():
                with self._outbox_cond:
                    while not self._stop.is_set() and (
                            not self._outbox or self._conn is None):
                        # A timeout rather than a pure wait so that a
                        # connection arriving (which does not notify from
                        # under this lock in every interleaving) is noticed
                        # promptly, and so shutdown is prompt even if a
                        # notify is missed entirely.
                        self._outbox_cond.wait(0.1)
                    if self._stop.is_set():
                        return
                    line = self._outbox.popleft()
                # Outside `_outbox_cond`, and `_conn_lock` is taken only after
                # it has been released: send() must never wait behind a write,
                # and the two locks are never held at once, so there is no
                # order between them to get wrong.
                with self._conn_lock:
                    conn = self._conn
                if conn is None:
                    # Retired between the pop and here.
                    self._count_drop("no connection")
                    continue
                try:
                    conn.sendall(line)
                except OSError as exc:
                    # Covers a closed/reset peer, a socket closed underneath
                    # us by _session, and socket.timeout from a daemon that
                    # has stopped reading. Debug, not warning: a daemon
                    # restart is ordinary, nothing here reaches the screen,
                    # and the reconnect loop is already logging the same event
                    # from the read side.
                    self._count_drop(exc)
        finally:
            self._wake_the_reader()

    def _wake_the_reader(self):
        """Shut the current connection down on the way out of `_send_loop`.

        Without this, `stop()` costs the reader's full socket timeout (5 s)
        whenever the daemon happens to be quiet -- which between folds it
        usually is -- because a thread blocked in a read cannot notice `_stop`
        until that read returns. A shutdown makes it return at once, with EOF.

        Done HERE, by the sender, and not by `stop()` itself, for two reasons
        that point the same way. The sender is the last thread that can still
        be *inside* this socket (parked in a `sendall` to a daemon that has
        stopped reading), so it is the one thread that knows when nobody is
        using it any more; a `stop()` that shut the socket down before joining
        the sender would be reaching past a thread it had not yet waited for.
        And it would make "did `stop()` actually wait for the sender?"
        unobservable, since the shutdown would unpark the sender at the same
        instant `stop()` stopped waiting on it -- exactly the shape of
        threading test that passes because the race never happened.
        """
        with self._conn_lock:
            conn = self._conn
        if conn is None:
            return
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass        # already closed by _session, or never connected

    def _count_drop(self, reason):
        log.debug("dropping client message: %s", reason)
        with self._outbox_cond:
            self.dropped_sends += 1
