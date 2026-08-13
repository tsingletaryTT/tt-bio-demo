import importlib
import sys

import pytest

from runner.workers import (
    CONTROL_FATAL, CONTROL_IDLE, CONTROL_READY, EVENT_FD, WorkerSpec,
    WorkerSpecError, control, is_control, worker_environ, worker_specs,
)


class _FakeSlot:
    """Stands in for tt_bio.runtime.WorkerSlot."""

    def __init__(self, device_id, host="quietbox"):
        self.device_id = device_id
        self.worker_id = f"{host}:tt:{device_id}"
        self.label = f"{host}:tt{device_id}"


@pytest.fixture
def fake_tt_bio(monkeypatch):
    """Replace the three tt-bio entry points with recorders.

    Deliberately NOT a stub that returns a fixed answer: each call's
    arguments are recorded, because half of what this module has to get
    right is *what it asks tt-bio for*.
    """
    calls = {}

    def detect(device_ids, num_devices, max_workers):
        calls["detect"] = (device_ids, num_devices, max_workers)
        return [0, 1, 2, 3]

    def build(accelerator, jobs, devices):
        calls["build"] = (accelerator, len(jobs), list(devices))
        return [_FakeSlot(d) for d in devices]

    def assignments(devices):
        calls["assign"] = list(devices)
        return {d: {"visible_devices": str(d), "logical_device_id": 0,
                    "mesh_graph_descriptor": "/mgd/p150.textproto"}
                for d in devices}

    import runner.workers as mod
    monkeypatch.setattr(mod, "_detect_tenstorrent_devices", detect, raising=False)
    monkeypatch.setattr(mod, "_build_local_workers", build, raising=False)
    monkeypatch.setattr(mod, "_worker_device_assignments", assignments, raising=False)
    return calls


def test_one_spec_per_detected_chip(fake_tt_bio):
    specs = worker_specs()
    assert [s.card for s in specs] == [0, 1, 2, 3]
    assert all(isinstance(s, WorkerSpec) for s in specs)


def test_each_spec_is_pinned_to_its_own_chip(fake_tt_bio):
    """The failure the spike existed to rule out: a worker that says chip 3
    and opens chip 0. visible_devices is the only thing that decides."""
    for spec in worker_specs():
        assert spec.visible_devices == str(spec.card)
        assert spec.logical_device_id == 0


def test_the_p300_mesh_graph_descriptor_reaches_every_spec(fake_tt_bio):
    """A lone P300 is a custom topology; without the 1x1 MGD the chip opens
    and then behaves strangely (spec)."""
    assert all(s.mesh_graph_descriptor == "/mgd/p150.textproto"
               for s in worker_specs())


def test_a_requested_device_list_is_passed_through_for_validation(fake_tt_bio):
    """detect_tenstorrent_devices is what turns a typo into a clear error.
    Filtering the list ourselves afterwards would skip that."""
    worker_specs(device_ids="0,2")
    assert fake_tt_bio["detect"][0] == "0,2"


def test_a_bad_device_id_becomes_a_WorkerSpecError(monkeypatch):
    import runner.workers as mod

    def detect(device_ids, num_devices, max_workers):
        raise ValueError("Requested Tenstorrent device id(s) [7] not available")

    monkeypatch.setattr(mod, "_detect_tenstorrent_devices", detect, raising=False)
    with pytest.raises(WorkerSpecError, match="7"):
        worker_specs(device_ids="7")


def test_no_chips_is_an_error_not_an_empty_booth(monkeypatch):
    import runner.workers as mod
    monkeypatch.setattr(mod, "_detect_tenstorrent_devices",
                        lambda *a, **k: [], raising=False)
    with pytest.raises(WorkerSpecError):
        worker_specs()


def test_the_environment_pins_visibility_before_the_interpreter_starts(fake_tt_bio):
    spec = worker_specs()[2]
    env = worker_environ(spec, log_root="/logs", n_workers=4, base={})
    assert env["TT_VISIBLE_DEVICES"] == "2"
    assert env["TT_BIO_LOGICAL_DEVICE_ID"] == "0"
    assert env["TT_MESH_GRAPH_DESC_PATH"] == "/mgd/p150.textproto"


def test_each_worker_gets_its_own_tt_metal_log_root(fake_tt_bio):
    """Four writers into one tree makes a crash unattributable, and makes
    the pruner's oldest-first sweep delete another worker's evidence."""
    roots = {worker_environ(s, log_root="/logs", n_workers=4,
                            base={})["TT_METAL_LOGS_PATH"]
             for s in worker_specs()}
    assert len(roots) == 4
    assert all(r.startswith("/logs/") for r in roots)


def test_the_inspector_stays_off_in_every_worker(fake_tt_bio):
    """runner/env.py turned Inspector off because it holds a log file open
    and writes 13-14 MB/s into it after unlink. Four workers is four of
    those."""
    env = worker_environ(worker_specs()[0], log_root="/logs", n_workers=4,
                         base={})
    assert env["TT_METAL_INSPECTOR"] == "0"


def test_host_threads_are_capped_for_the_number_of_workers(fake_tt_bio):
    """tt-bio documents this exact case: 'an external launcher runs one
    single-card predict per chip; each process then sees n_workers == 1,
    sizes its pools to all cores, and N co-resident folds oversubscribe the
    host N-fold.' We are that launcher."""
    one = worker_environ(worker_specs()[0], log_root="/logs", n_workers=1,
                         base={})
    four = worker_environ(worker_specs()[0], log_root="/logs", n_workers=4,
                          base={})
    assert int(four["OMP_NUM_THREADS"]) < int(one["OMP_NUM_THREADS"])
    assert int(four["OMP_NUM_THREADS"]) >= 1


def test_an_operator_set_variable_is_never_clobbered(fake_tt_bio):
    """Same guarantee runner_environ already makes, for the same reason."""
    env = worker_environ(worker_specs()[0], log_root="/logs", n_workers=4,
                         base={"TT_METAL_INSPECTOR": "1"})
    assert env["TT_METAL_INSPECTOR"] == "1"


def test_visibility_is_never_left_to_an_operator_to_get_wrong(fake_tt_bio):
    """The one exception to the rule above. An ambient TT_VISIBLE_DEVICES
    inherited from the parent's shell would silently pin every worker to
    the same chip -- and detect_tenstorrent_devices itself honours the
    ambient value, so a stale one narrows the whole booth to one card."""
    env = worker_environ(worker_specs()[3], log_root="/logs", n_workers=4,
                         base={"TT_VISIBLE_DEVICES": "0"})
    assert env["TT_VISIBLE_DEVICES"] == "3"


def test_control_lines_are_distinguishable_from_protocol_events():
    from protocol.events import EVENT_TYPES
    for kind in (CONTROL_READY, CONTROL_IDLE, CONTROL_FATAL):
        assert kind not in EVENT_TYPES
        assert is_control({"type": kind})
    assert not is_control({"type": "job_done", "job_id": "j1"})
    assert not is_control({})


def test_control_carries_its_fields():
    assert control(CONTROL_IDLE, job_id="j1") == {"type": CONTROL_IDLE,
                                                  "job_id": "j1"}


def test_the_event_fd_is_not_a_standard_stream():
    """tt-metal writes to fd 1 and fd 2 from C++. An event stream on either
    is a shredded event stream."""
    assert EVENT_FD not in (0, 1, 2)


def test_the_module_imports_without_tt_bio(monkeypatch):
    """The parent must not pay for ttnn just to import this module, and the
    134 existing runner tests must not either. tt-bio is imported lazily,
    inside the functions that need it."""
    for name in [m for m in sys.modules if m == "tt_bio" or m.startswith("tt_bio.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "tt_bio", None)   # importing it now raises
    monkeypatch.delitem(sys.modules, "runner.workers", raising=False)
    importlib.import_module("runner.workers")          # must not raise
