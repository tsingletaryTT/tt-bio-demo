# tt-bio-demo Phase 3a: The Runner Daemon — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `tt-bio-demod`, the compute daemon that folds proteins on Tenstorrent hardware and streams the live diffusion trajectory to the UI over the existing Unix-socket protocol.

**Architecture:** A long-lived process in `venv-runner` that opens the device once, holds the model resident across folds, and serves the same newline-delimited JSON protocol the mock runner already speaks — so the UI, which is already built and tested, connects to it unchanged. The trajectory is obtained by patching `tt_bio.protenix.edm_sample`, guarded by a test that fails loudly if that private surface shifts.

**Tech Stack:** Python 3.12 in `.venvs/venv-runner` (torch, ttnn, tt-bio 0.6.2, vendored SFPI 7.35.3), the project's own `protocol/events.py`, pytest.

**Spec:** [`../specs/2026-08-10-tt-bio-demo-design.md`](../specs/2026-08-10-tt-bio-demo-design.md) — §2 (architecture), §3 (protocol), §6 (failure handling).
**Spike findings this plan is built on:** [`../../spike-real-fold.md`](../../spike-real-fold.md) — read it before Task 1. Every number below came from it.

## Global Constraints

- **Run everything with `.venvs/venv-runner/bin/python3`.** Never bare `python3` — a Tenstorrent virtualenv on `$PATH` lacks these packages. The UI venv (`.venvs/venv-ui`) is a *different* environment and cannot import torch or tt-bio.
- **`protocol/events.py` is shared by both environments and must keep importing only stdlib and numpy.** Do not add dependencies to it.
- **The runner must never crash the UI.** It is a separate process precisely so a wedged fold cannot take the screen down. Every failure path ends in a `job_error` event and a live socket, not a dead process.
- **Nothing the runner emits may be displayed raw.** `job_error.message` goes to logs; the UI shows neutral copy. Keep messages diagnostic, not user-facing.
- **The device is opened exactly once per daemon lifetime.** Every `get_device()` call emits ~40 lines of INFO to stderr, and models must stay resident for the second fold to be fast.
- **`PROTOCOL_VERSION = 1`.** The UI refuses a mismatched `hello` and stops reconnecting, so bumping it is a breaking change requiring both sides to ship together.
- **pLDDT is reported 0–100, not 0–1.** tt-bio returns `conf["plddt"]` as a fraction; multiplying by 100 is the runner's job.
- **Frames are subsampled to ~30 per fold.** A real fold produces 201. This is about socket and render bandwidth, not about protecting the sampler — coordinates are already host tensors between steps.
- **Never run `tt-bio install-deps`, reset cards (`tt-smi -r`), or touch the system SFPI.** This machine is shared. Card reset stays a documented manual step per spec §6.
- Commit after every task with conventional-commit prefixes (`feat:`, `fix:`, `test:`, `chore:`).

## Measured facts from the spike (do not re-derive)

| Fact | Value |
|---|---|
| `dump_fn` coords | `torch.Size([1, 154, 3])`, `torch.float32`, `device=cpu`, `requires_grad=False` |
| Calls per fold | 201 — step `-1` (initial noise draw) then `0..199` |
| Atoms | 154 all-atom, model-internal order, for a 20-residue input |
| Radius of gyration | 4357.7 Å → 6.99 Å, monotonic, converged by ~step 150 |
| Fold wall clock | 5.73 s first, 4.36 s second (resident), model load 2.45 s |
| Stages tt-bio emits | **only** `trunk` (10 calls, `total=10`) and `diffusion` (200 calls, `total=200`) |
| Inspector/Watcher logs | **121 MB for two folds**, written relative to CWD |
| `TT_METAL_WATCHER=0` | Does **not** disable — it hung the box for two minutes |

## File Structure

| File | Responsibility |
|---|---|
| `runner/env.py` | Build the tt-metal environment so logs go to a bounded absolute path, never CWD. Pure function, no side effects. |
| `runner/dump_tap.py` | Install/remove the `edm_sample` trajectory patch. Isolates the one fragile coupling into a single small file with its own guard test. |
| `runner/server.py` | Unix-socket server. Accepts UI clients, broadcasts protocol events. Production counterpart to `runner/mock.py`. |
| `runner/folder.py` | Owns the device and the resident model. Runs one fold, emits the full event sequence. |
| `runner/queue.py` | Priority job queue; at most one in-flight job per card. |
| `runner/preflight.py` | Verifies offline readiness before folding is allowed. |
| `runner/daemon.py` | Wires the above into `tt-bio-demod` and owns the process lifecycle. |
| `tests/unit/` | Headless tests — no device, no torch import where avoidable. |
| `tests/integration/` | Hardware-gated tests, skipped when no card is available. |

Deliberate boundary: `env.py`, `queue.py`, `preflight.py` and the event-shaping helpers in `folder.py` are pure and testable with no hardware. Only the fold itself needs a card, and those tests are marked and skippable.

---

### Task 1: Contain and bound tt-metal's log output

**Files:**
- Create: `runner/env.py`
- Test: `tests/unit/test_runner_env.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `runner_environ(log_root, base=None) -> dict[str, str]`; `LOG_ROOT_VAR: str`; `prune_log_root(log_root, max_bytes, *, dry_run=False) -> tuple[int, list[str]]` returning `(bytes_freed, paths_removed)`; `log_root_size(log_root) -> int`.

Two halves, both needed. Pinning the path stops gigabytes landing in whatever directory the daemon happened to start in. It does **not** stop them accumulating — tt-metal has no size cap of its own, so an unattended booth still fills its disk, just tidily. The daemon therefore enforces a budget itself.

This is first because the spike measured **121 MB of Inspector/Watcher logs for two folds**, written relative to whatever the daemon's CWD happens to be. At one fold every ~45 s for a conference day that is gigabytes, and a booth machine that fills its disk overnight is a dead booth. Every later task runs folds, so containment must exist before they do.

**A warning the spike paid for:** `TT_METAL_WATCHER=0` does *not* mean "off" — it hung the box for two minutes with a busy-poll. Do not assume any of these variables mean what their name suggests. Step 1 is discovery, not implementation.

- [ ] **Step 1: Find out what the variables actually do**

Run each of these in turn and record what happens — whether a `generated/` tree appears, how big it gets, and whether the process behaves normally. Use a scratch CWD each time so results are unambiguous:

```bash
cd "$(mktemp -d)" && TT_METAL_LOGS_PATH=/tmp/probe-logs \
  /home/ttuser/code/tt-bio-demo/.venvs/venv-runner/bin/python3 -c "
import ttnn; d=ttnn.open_device(device_id=0); ttnn.close_device(d); print('ok')
" 2>&1 | tail -3; ls
```

Try at minimum: `TT_METAL_LOGS_PATH` set to an absolute path, and `TT_METAL_INSPECTOR=0`. **Time-box each probe** and do not retry `TT_METAL_WATCHER=0` — it is known to hang.

Confirm the variable you settle on actually appears in the shipped binaries before trusting it — `strings` over `libtt_metal.so` and the other `.so` files in `venv-runner`'s ttnn install is a two-minute check that distinguishes "this variable works" from "this variable is ignored and my test passes anyway."

Write what you find into the module docstring of `runner/env.py`, with the measured `generated/` sizes. A future reader must be able to see the evidence, because the obvious reading of these variable names is wrong.

- [ ] **Step 2: Write the failing test**

Create `tests/unit/test_runner_env.py`:

```python
import os

from runner.env import LOG_ROOT_VAR, runner_environ


def test_inspector_log_path_is_absolute_and_under_the_log_root(tmp_path):
    env = runner_environ(tmp_path / "logs", base={})
    value = env[LOG_ROOT_VAR]
    assert os.path.isabs(value), f"{LOG_ROOT_VAR} must be absolute, got {value!r}"
    assert str(tmp_path / "logs") in value


def test_relative_log_root_is_resolved_to_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = runner_environ("logs", base={})
    assert os.path.isabs(env[LOG_ROOT_VAR])


def test_base_environment_is_not_mutated():
    base = {"PATH": "/usr/bin"}
    runner_environ("/tmp/logs", base=base)
    assert base == {"PATH": "/usr/bin"}, "runner_environ must not mutate its input"


def test_base_environment_is_carried_through():
    env = runner_environ("/tmp/logs", base={"PATH": "/usr/bin", "HOME": "/home/x"})
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/x"


def test_caller_supplied_log_path_is_not_silently_overridden():
    # An operator who sets this deliberately should win; we only fill the gap.
    env = runner_environ("/tmp/logs", base={LOG_ROOT_VAR: "/operator/choice"})
    assert env[LOG_ROOT_VAR] == "/operator/choice"


def test_defaults_to_the_process_environment_when_no_base_given(monkeypatch):
    monkeypatch.setenv("TTBIO_DEMO_MARKER", "present")
    env = runner_environ("/tmp/logs")
    assert env["TTBIO_DEMO_MARKER"] == "present"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/test_runner_env.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runner.env'`

- [ ] **Step 4: Write the implementation**

Create `runner/env.py`. Fill the docstring with what Step 1 actually measured — the text below marks where:

```python
"""tt-metal environment containment for the runner daemon.

tt-metal's Inspector subsystem writes structured logs for every kernel it runs,
to a path that is *relative to the process CWD* by default. Measured during the
Phase 3a spike: two 200-step folds produced 121 MB under `generated/`. A booth
running one fold every ~45 seconds for a conference day would produce gigabytes
and eventually fill the disk, so the daemon pins the location to an absolute
path it owns and can rotate.

Measured behaviour of the relevant variables (see docs/spike-real-fold.md and
Task 1 Step 1 of the Phase 3a plan):

    <FILL IN from Step 1: what each probed variable actually did, with sizes>

Do not assume these variables mean what their names suggest. `TT_METAL_WATCHER=0`
does not disable the Watcher — it was observed to hang for two minutes in a
busy-poll. It is deliberately not set here.
"""

import os
from pathlib import Path

# tt-metal reads this to decide where its logs are written. Absolute paths only.
#
# NOTE (corrected during execution): this plan originally specified
# TT_METAL_INSPECTOR_LOG_PATH, which does not exist in this tt-metal build —
# zero matches across every shared object in site-packages, and no measurable
# effect. It would have passed every unit test while containing nothing. The
# real lever is TT_METAL_LOGS_PATH (one match, in libtt_metal.so), verified by
# a device probe that left the CWD empty.
LOG_ROOT_VAR = "TT_METAL_LOGS_PATH"


def runner_environ(log_root, base=None):
    """Return an environment mapping with tt-metal's log output pinned.

    `log_root` may be relative; it is resolved against the current directory so
    the daemon's own CWD can never leak into where gigabytes get written. An
    operator who has already set LOG_ROOT_VAR keeps their choice.
    """
    env = dict(os.environ if base is None else base)
    env.setdefault(LOG_ROOT_VAR, str(Path(log_root).resolve()))
    return env
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/test_runner_env.py -v`
Expected: PASS, 6 tests

- [ ] **Step 6: Prove it against a real device open**

The unit tests only prove the dict is shaped right. Prove the containment works:

```bash
cd "$(mktemp -d)" && pwd && \
env $(cd /home/ttuser/code/tt-bio-demo && .venvs/venv-runner/bin/python3 -c "
from runner.env import runner_environ
for k,v in runner_environ('/tmp/ttbio-demo-logs', base={}).items(): print(f'{k}={v}')") \
  /home/ttuser/code/tt-bio-demo/.venvs/venv-runner/bin/python3 -c "
import ttnn; d=ttnn.open_device(device_id=0); ttnn.close_device(d)" 2>/dev/null; \
echo "--- files left in CWD:"; ls -A; echo "--- log root:"; du -sh /tmp/ttbio-demo-logs 2>/dev/null
```

Expected: the scratch CWD is empty (or contains nothing tt-metal wrote), and any output landed under `/tmp/ttbio-demo-logs`. **If `generated/` still appears in the CWD, say so in your report rather than proceeding** — that means the variable does not do what Step 1 suggested and the containment strategy needs rethinking before any later task runs folds in a loop.

- [ ] **Step 7: Write the failing test for the size budget**

Append to `tests/unit/test_runner_env.py`:

```python
import os
import time

from runner.env import log_root_size, prune_log_root


def _file(root, name, size, age_s=0):
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_bytes(b"x" * size)
    if age_s:
        old = time.time() - age_s
        os.utime(p, (old, old))
    return p


def test_size_of_a_missing_root_is_zero(tmp_path):
    assert log_root_size(tmp_path / "nope") == 0


def test_size_counts_files_in_subdirectories(tmp_path):
    _file(tmp_path / "inspector", "a.yaml", 1000)
    _file(tmp_path / "inspector" / "deep", "b.yaml", 500)
    assert log_root_size(tmp_path) == 1500


def test_nothing_is_removed_when_under_budget(tmp_path):
    _file(tmp_path, "a.yaml", 100)
    freed, removed = prune_log_root(tmp_path, max_bytes=10_000)
    assert freed == 0 and removed == []
    assert (tmp_path / "a.yaml").exists()


def test_oldest_files_go_first_until_under_budget(tmp_path):
    _file(tmp_path, "old.yaml", 1000, age_s=900)
    _file(tmp_path, "mid.yaml", 1000, age_s=600)
    _file(tmp_path, "new.yaml", 1000, age_s=1)
    freed, removed = prune_log_root(tmp_path, max_bytes=1500)
    assert not (tmp_path / "old.yaml").exists()
    assert not (tmp_path / "mid.yaml").exists()
    assert (tmp_path / "new.yaml").exists(), "the newest log must survive"
    assert freed == 2000
    assert sorted(os.path.basename(p) for p in removed) == ["mid.yaml", "old.yaml"]


def test_dry_run_reports_without_deleting(tmp_path):
    _file(tmp_path, "old.yaml", 1000, age_s=900)
    _file(tmp_path, "new.yaml", 1000, age_s=1)
    freed, removed = prune_log_root(tmp_path, max_bytes=1500, dry_run=True)
    assert freed == 1000
    assert len(removed) == 1
    assert (tmp_path / "old.yaml").exists(), "dry run must not delete"


def test_the_root_directory_itself_is_never_removed(tmp_path):
    _file(tmp_path, "a.yaml", 5000)
    prune_log_root(tmp_path, max_bytes=0)
    assert tmp_path.is_dir()


def test_a_missing_root_is_not_an_error(tmp_path):
    freed, removed = prune_log_root(tmp_path / "nope", max_bytes=100)
    assert freed == 0 and removed == []


def test_a_symlink_pointing_outside_the_root_is_never_followed(tmp_path):
    """The one that matters: this function deletes files."""
    outside = tmp_path / "precious"
    outside.mkdir()
    victim = outside / "do-not-delete.txt"
    victim.write_bytes(b"y" * 5000)

    root = tmp_path / "logs"
    root.mkdir()
    _file(root, "a.yaml", 100)
    (root / "escape").symlink_to(outside)

    prune_log_root(root, max_bytes=0)
    assert victim.exists(), "pruning escaped the log root via a symlink"


def test_refuses_a_root_that_is_not_a_directory(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    freed, removed = prune_log_root(f, max_bytes=0)
    assert freed == 0 and removed == []
```

- [ ] **Step 8: Run test to verify it fails**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/test_runner_env.py -v`
Expected: the six original tests pass; the nine new ones FAIL with `ImportError: cannot import name 'prune_log_root'`

- [ ] **Step 9: Implement the budget**

Append to `runner/env.py`:

```python
def log_root_size(log_root):
    """Total bytes of regular files under `log_root`. Missing root counts as 0."""
    root = Path(log_root)
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def prune_log_root(log_root, max_bytes, *, dry_run=False):
    """Delete oldest log files until the root fits in `max_bytes`.

    Returns (bytes_freed, paths_removed).

    This function deletes files, so it is deliberately narrow about what it will
    touch: regular files only, never symlinks (so a link inside the root cannot
    be used to reach anything outside it), never the root directory itself, and
    nothing at all if the root is missing or is not a directory. Oldest-first by
    mtime, so the newest logs — the ones useful for diagnosing whatever just
    happened — are the last to go.
    """
    root = Path(log_root)
    if not root.is_dir():
        return 0, []

    entries = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))

    total = sum(size for _, size, _ in entries)
    if total <= max_bytes:
        return 0, []

    entries.sort(key=lambda e: e[0])          # oldest first
    freed, removed = 0, []
    for _, size, path in entries:
        if total - freed <= max_bytes:
            break
        if not dry_run:
            try:
                path.unlink()
            except OSError:
                log.warning("could not remove %s while pruning logs", path)
                continue
        freed += size
        removed.append(str(path))

    if removed:
        log.info("pruned %d log file(s), freed %.1f MB%s",
                 len(removed), freed / 1e6, " (dry run)" if dry_run else "")
    return freed, removed
```

Add `import logging` and `log = logging.getLogger(__name__)` at the top of the module if not already present.

- [ ] **Step 10: Run test to verify it passes**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/test_runner_env.py -v`
Expected: PASS, 15 tests

- [ ] **Step 11: Commit**

```bash
git add runner/env.py tests/unit/test_runner_env.py
git commit -m "feat(runner): pin tt-metal logs to an absolute root and cap their size"
```

---

### Task 2: The trajectory tap, and a guard that fails loudly

**Files:**
- Create: `runner/dump_tap.py`
- Test: `tests/unit/test_dump_tap.py`

**Interfaces:**
- Consumes: `tt_bio.protenix` (imported lazily inside functions, so the module itself imports without torch).
- Produces: `install_trajectory_tap(on_frame) -> object` (returns a handle); `remove_trajectory_tap(handle) -> None`; `TapUnavailable(Exception)`; `check_tap_supported() -> None` (raises `TapUnavailable` with a specific message, returns `None` when fine).

`on_frame` is called as `on_frame(sample: int, step: int, coords: numpy.ndarray)` with `coords` shaped `(N, 3)` float32 — matching the wire format's expectation, so the caller never handles torch tensors.

**Why this file exists at all.** `OpenDDE.fold` accepts a public `dump_fn`; `Protenix.fold` does not, even though the `edm_sample` it calls internally already takes one. Getting a trajectory out therefore means replacing the module-level `tt_bio.protenix.edm_sample` — a private implementation detail. Isolating that into one small file means one place to change when upstream lands the patch in `docs/upstream/protenix-dump-fn/`.

**The failure mode that matters.** If a tt-bio upgrade moves, renames, or pre-empts `edm_sample`, the fold still succeeds and produces a correct structure — it just silently stops emitting frames. The demo would appear to work while its headline feature is dead. That is why `check_tap_supported()` exists and why the guard checks *two* directions: the symbol being gone, and the symbol gaining a caller that passes `dump_fn` itself.

That second case is real: the upstream patch this project prepared makes `Protenix.fold` always pass `dump_fn=` explicitly. A tap written with `kwargs.setdefault("dump_fn", ...)` would silently stop intercepting the moment that lands.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_dump_tap.py`:

```python
import sys
import types

import numpy as np
import pytest

from runner.dump_tap import (
    TapUnavailable,
    check_tap_supported,
    install_trajectory_tap,
    remove_trajectory_tap,
)


def _fake_protenix(monkeypatch, *, fold_passes_dump_fn=False, with_edm=True):
    """Install a stand-in tt_bio.protenix so these tests need no torch or hardware."""
    mod = types.ModuleType("tt_bio.protenix")

    if with_edm:
        def edm_sample(diffusion_module, cond, n_atoms, *, dump_fn=None, **kw):
            # Two steps of a two-atom "trajectory", shaped like the real thing.
            for step in (-1, 0):
                if dump_fn is not None:
                    dump_fn(step, np.full((1, 2, 3), float(step), dtype=np.float32))
            return "coords-sentinel"
        mod.edm_sample = edm_sample

    def fold():
        kwargs = {"dump_fn": None} if fold_passes_dump_fn else {}
        return sys.modules["tt_bio.protenix"].edm_sample(None, None, 2, **kwargs)
    mod.fold = fold

    pkg = types.ModuleType("tt_bio")
    pkg.protenix = mod
    monkeypatch.setitem(sys.modules, "tt_bio", pkg)
    monkeypatch.setitem(sys.modules, "tt_bio.protenix", mod)
    return mod


def test_check_passes_when_edm_sample_accepts_dump_fn(monkeypatch):
    _fake_protenix(monkeypatch)
    assert check_tap_supported() is None


def test_check_raises_when_edm_sample_is_missing(monkeypatch):
    _fake_protenix(monkeypatch, with_edm=False)
    with pytest.raises(TapUnavailable, match="edm_sample"):
        check_tap_supported()


def test_check_raises_when_edm_sample_lost_its_dump_fn_parameter(monkeypatch):
    mod = _fake_protenix(monkeypatch)
    mod.edm_sample = lambda diffusion_module, cond, n_atoms, **kw: None
    with pytest.raises(TapUnavailable, match="dump_fn"):
        check_tap_supported()


def test_tap_receives_every_step_as_an_n_by_3_float32_array(monkeypatch):
    mod = _fake_protenix(monkeypatch)
    seen = []
    handle = install_trajectory_tap(lambda s, st, c: seen.append((s, st, c)))
    try:
        mod.fold()
    finally:
        remove_trajectory_tap(handle)

    assert [(s, st) for s, st, _ in seen] == [(0, -1), (0, 0)]
    for _, _, coords in seen:
        assert coords.shape == (2, 3)
        assert coords.dtype == np.float32


def test_tap_intercepts_even_when_the_caller_passes_dump_fn_itself(monkeypatch):
    # The upstream patch makes Protenix.fold always pass dump_fn=. A tap using
    # setdefault would silently stop firing; this pins that it does not.
    mod = _fake_protenix(monkeypatch, fold_passes_dump_fn=True)
    seen = []
    handle = install_trajectory_tap(lambda s, st, c: seen.append(st))
    try:
        mod.fold()
    finally:
        remove_trajectory_tap(handle)
    assert seen == [-1, 0], "tap was pre-empted by the caller's own dump_fn"


def test_removing_the_tap_restores_the_original_function(monkeypatch):
    mod = _fake_protenix(monkeypatch)
    original = mod.edm_sample
    handle = install_trajectory_tap(lambda s, st, c: None)
    assert mod.edm_sample is not original
    remove_trajectory_tap(handle)
    assert mod.edm_sample is original


def test_removing_twice_is_harmless(monkeypatch):
    mod = _fake_protenix(monkeypatch)
    handle = install_trajectory_tap(lambda s, st, c: None)
    remove_trajectory_tap(handle)
    remove_trajectory_tap(handle)


def test_the_wrapped_function_still_returns_what_the_caller_expects(monkeypatch):
    mod = _fake_protenix(monkeypatch)
    handle = install_trajectory_tap(lambda s, st, c: None)
    try:
        assert mod.fold() == "coords-sentinel"
    finally:
        remove_trajectory_tap(handle)


def test_a_raising_callback_does_not_break_the_fold(monkeypatch):
    # A bug in the consumer must not take down a fold that is otherwise fine.
    mod = _fake_protenix(monkeypatch)

    def boom(sample, step, coords):
        raise ValueError("consumer bug")

    handle = install_trajectory_tap(boom)
    try:
        assert mod.fold() == "coords-sentinel"
    finally:
        remove_trajectory_tap(handle)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/test_dump_tap.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runner.dump_tap'`

- [ ] **Step 3: Write the implementation**

Create `runner/dump_tap.py`:

```python
"""Extract the live diffusion trajectory from Protenix.

`OpenDDE.fold` exposes a public `dump_fn`; `Protenix.fold` does not, even though
the `edm_sample` it calls internally already accepts one. So the only way to
observe per-step coordinates is to replace the module-level
`tt_bio.protenix.edm_sample` before calling `fold()`.

That is a coupling to a private surface, deliberately confined to this file. A
patch adding the public parameter is prepared in
`docs/upstream/protenix-dump-fn/`; when it lands upstream, this module is the
only thing that has to change.

The failure this module exists to prevent: if `edm_sample` moves, is renamed, or
gains a caller that passes `dump_fn` itself, the fold still succeeds and produces
a correct structure — it just stops emitting frames. The demo would look like it
was working while its headline feature was dead. `check_tap_supported()` turns
that into a loud, specific error at startup instead.
"""

import inspect
import logging

log = logging.getLogger(__name__)


class TapUnavailable(Exception):
    """tt-bio's internals no longer match what the trajectory tap expects."""


def _protenix():
    import tt_bio.protenix as protenix  # imported lazily: pulls in torch
    return protenix


def check_tap_supported():
    """Raise TapUnavailable with an actionable message if the tap cannot work."""
    protenix = _protenix()

    fn = getattr(protenix, "edm_sample", None)
    if fn is None or not callable(fn):
        raise TapUnavailable(
            "tt_bio.protenix.edm_sample is missing or not callable; the trajectory "
            "tap targets it directly. tt-bio's internals have changed — check "
            "whether Protenix.fold now takes a public dump_fn (see "
            "docs/upstream/protenix-dump-fn/) and switch to it."
        )

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError) as exc:
        raise TapUnavailable(f"cannot inspect tt_bio.protenix.edm_sample: {exc}") from exc

    if "dump_fn" not in params:
        raise TapUnavailable(
            "tt_bio.protenix.edm_sample no longer accepts dump_fn; the trajectory "
            "tap cannot observe denoising steps through it."
        )
    return None


def install_trajectory_tap(on_frame):
    """Route every denoising step to `on_frame(sample, step, coords)`.

    `coords` arrives as an (N, 3) float32 numpy array, already on the host.

    The wrapper *overrides* any dump_fn the caller passes rather than deferring
    to it. Deferring (e.g. kwargs.setdefault) would silently stop intercepting
    the moment tt-bio's own code starts passing the parameter — which is exactly
    what the prepared upstream patch does.
    """
    import numpy as np

    check_tap_supported()
    protenix = _protenix()
    original = protenix.edm_sample

    def tapped(*args, **kwargs):
        caller_dump_fn = kwargs.get("dump_fn")

        def relay(step, x):
            if caller_dump_fn is not None:
                try:
                    caller_dump_fn(step, x)
                except Exception:
                    log.exception("caller's dump_fn raised; continuing")
            try:
                coords = np.asarray(
                    x.detach().cpu().numpy() if hasattr(x, "detach") else x,
                    dtype=np.float32,
                ).reshape(-1, 3)
                on_frame(0, int(step), coords)
            except Exception:
                # A consumer bug must never abort a fold that is otherwise fine.
                log.exception("trajectory callback raised; continuing the fold")

        kwargs["dump_fn"] = relay
        return original(*args, **kwargs)

    protenix.edm_sample = tapped
    return (protenix, original)


def remove_trajectory_tap(handle):
    """Restore the original edm_sample. Safe to call more than once."""
    protenix, original = handle
    if getattr(protenix, "edm_sample", None) is not original:
        protenix.edm_sample = original
```

Note `on_frame` is called with `sample=0`: the daemon folds one sample at a time (`n_sample=1`), which the spike confirmed is what the real run does. If multi-sample is ever needed, this is where the sample index gets threaded through.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/test_dump_tap.py -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Verify the guard against the real tt-bio**

The tests above use a stand-in. Confirm the guard agrees with reality:

```bash
cd /home/ttuser/code/tt-bio-demo && .venvs/venv-runner/bin/python3 -c "
from runner.dump_tap import check_tap_supported
check_tap_supported()
print('tap supported against the installed tt-bio')
"
```

Expected: prints the confirmation. If it raises, the installed tt-bio differs from what the spike measured — report that rather than weakening the guard.

- [ ] **Step 6: Commit**

```bash
git add runner/dump_tap.py tests/unit/test_dump_tap.py
git commit -m "feat(runner): tap Protenix's diffusion trajectory, with a loud guard"
```

---

### Task 3: Frame subsampling and event shaping

**Files:**
- Create: `runner/shaping.py`
- Test: `tests/unit/runner/test_shaping.py`

**Interfaces:**
- Consumes: `protocol.events.pack_coords`.
- Produces: `select_frame_steps(total: int, target: int = 30) -> list[int]`; `frame_event(job_id: str, step: int, total: int, coords) -> dict`; `plddt_to_percent(value: float) -> float`; `STAGE_ORDER: tuple[str, ...]`.

Pulled into its own module because it is pure, it is where two of the spike's findings live, and keeping it out of `folder.py` means the fold logic stays readable and this logic stays testable without a card.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/runner/test_shaping.py`:

```python
import numpy as np
import pytest

from protocol.events import EVENT_TYPES, unpack_coords
from runner.shaping import (
    STAGE_ORDER,
    frame_event,
    plddt_to_percent,
    select_frame_steps,
)


def test_selects_about_the_target_number_of_frames():
    steps = select_frame_steps(201, target=30)
    assert 28 <= len(steps) <= 30


def test_always_keeps_the_first_and_last_step():
    steps = select_frame_steps(201, target=30)
    assert steps[0] == 0
    assert steps[-1] == 200


def test_selected_steps_are_sorted_and_unique():
    steps = select_frame_steps(201, target=30)
    assert steps == sorted(steps)
    assert len(steps) == len(set(steps))


def test_a_short_run_keeps_every_step_rather_than_inventing_any():
    steps = select_frame_steps(5, target=30)
    assert steps == [0, 1, 2, 3, 4]


def test_single_step_run_is_handled():
    assert select_frame_steps(1, target=30) == [0]


def test_zero_steps_selects_nothing():
    assert select_frame_steps(0, target=30) == []


def test_frame_event_round_trips_the_coordinates():
    coords = np.array([[1.5, -2.0, 3.25], [0.0, 0.5, -1.0]], dtype=np.float32)
    event = frame_event("j1", step=7, total=200, coords=coords)
    np.testing.assert_allclose(unpack_coords(event["coords_b64"]), coords, atol=1e-6)


def test_frame_event_matches_the_wire_contract():
    coords = np.zeros((154, 3), dtype=np.float32)
    event = frame_event("j1", step=7, total=200, coords=coords)
    assert event["type"] == "frame"
    assert event["type"] in EVENT_TYPES
    assert event["job_id"] == "j1"
    assert event["step"] == 7
    assert event["total"] == 200
    assert event["n_atoms"] == 154


def test_plddt_is_scaled_from_fraction_to_percent():
    # tt-bio returns conf["plddt"] as a fraction; the wire format is 0-100.
    assert plddt_to_percent(0.95) == pytest.approx(95.0)
    assert plddt_to_percent(0.4837) == pytest.approx(48.37)


def test_plddt_already_in_percent_is_left_alone():
    # Guard against double-scaling if tt-bio ever changes units.
    assert plddt_to_percent(95.0) == pytest.approx(95.0)


def test_stage_order_matches_the_protocol_table():
    assert STAGE_ORDER == ("msa", "prep", "trunk", "diffusion", "confidence", "saving")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/runner/test_shaping.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runner.shaping'`

- [ ] **Step 3: Write the implementation**

Create `runner/shaping.py`:

```python
"""Turn raw fold output into wire events.

Pure functions only — no device, no tt-bio import — so all of this is testable
without hardware.

Two of these encode findings from the Phase 3a spike:

* A real fold emits 201 denoising steps. The UI needs about 30; the rest are
  bandwidth for no visual gain. Note this is *not* about protecting the sampler
  from an expensive device-to-host copy — the spike established there isn't one,
  since coordinates are already host tensors between steps.
* tt-bio reports pLDDT as a fraction. The wire format says 0-100. Without the
  scale, `job_done.mean_plddt` silently reads 0.95 instead of 95.
"""

import numpy as np

from protocol.events import pack_coords

# The full vocabulary the protocol promises. tt-bio itself only ever reports
# `trunk` and `diffusion`; the other four are emitted by the daemon bracketing
# the work it does around the fold.
STAGE_ORDER = ("msa", "prep", "trunk", "diffusion", "confidence", "saving")


def select_frame_steps(total, target=30):
    """Pick ~`target` evenly spaced step indices out of `total`, keeping the ends.

    Fewer steps than the target keeps every one — we never invent frames.
    """
    if total <= 0:
        return []
    if total <= target:
        return list(range(total))
    picks = np.linspace(0, total - 1, target).round().astype(int)
    return sorted(set(int(p) for p in picks))


def frame_event(job_id, step, total, coords):
    """Build a `frame` event from one denoising step's coordinates."""
    arr = np.asarray(coords, dtype=np.float32).reshape(-1, 3)
    return {
        "type": "frame",
        "job_id": job_id,
        "step": int(step),
        "total": int(total),
        "n_atoms": int(arr.shape[0]),
        "coords_b64": pack_coords(arr),
    }


def plddt_to_percent(value):
    """Scale tt-bio's fractional pLDDT to the wire format's 0-100.

    Values already above 1.0 are passed through, so a future tt-bio that changes
    units cannot cause silent double-scaling.
    """
    v = float(value)
    return v * 100.0 if v <= 1.0 else v
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/runner/test_shaping.py -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Commit**

```bash
git add runner/shaping.py tests/unit/runner/test_shaping.py
git commit -m "feat(runner): subsample frames and scale pLDDT for the wire format"
```

---

### Task 4: The event server

**Files:**
- Create: `runner/server.py`
- Test: `tests/unit/runner/test_runner_server.py`

**Interfaces:**
- Consumes: `protocol.events.encode`, `PROTOCOL_VERSION`.
- Produces: `EventServer(socket_path, hello_factory)` with `.start()`, `.stop()`, `.broadcast(event: dict) -> int` (returns the number of clients it reached), `.client_count` property, and attribute `.socket_path`.

`hello_factory` is a zero-argument callable returning the `hello` event payload, called fresh per connection so a late-joining UI sees current card and model state rather than a stale snapshot.

This is the production counterpart to `runner/mock.py`. It differs in the way that matters: the mock replays a fixed script to each client from the beginning, while this broadcasts live events to whoever is connected. A client that connects mid-fold gets `hello` and then joins in progress.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/runner/test_runner_server.py`:

```python
import socket
import threading

from protocol.events import PROTOCOL_VERSION, decode
from runner.server import EventServer


def _hello():
    return {"type": "hello", "version": PROTOCOL_VERSION, "cards": [0],
            "models": ["protenix-v2"], "preflight": "ok"}


def _connect(path):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5.0)
    client.connect(path)
    return client


def _wait_for_clients(server, n, timeout=5.0):
    deadline = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if server.client_count >= n:
            return True
        deadline.wait(0.02)
    return False


def test_a_connecting_client_receives_hello_first(tmp_path):
    server = EventServer(str(tmp_path / "r.sock"), _hello)
    server.start()
    try:
        client = _connect(server.socket_path)
        with client.makefile("rb") as stream:
            first = decode(stream.readline())
        assert first["type"] == "hello"
        assert first["version"] == PROTOCOL_VERSION
    finally:
        client.close()
        server.stop()


def test_hello_is_built_fresh_for_each_connection(tmp_path):
    calls = []

    def counting_hello():
        calls.append(1)
        return _hello()

    server = EventServer(str(tmp_path / "r.sock"), counting_hello)
    server.start()
    try:
        for _ in range(2):
            client = _connect(server.socket_path)
            with client.makefile("rb") as stream:
                stream.readline()
            client.close()
    finally:
        server.stop()
    assert len(calls) == 2, "hello must reflect current state, not a cached snapshot"


def test_broadcast_reaches_a_connected_client(tmp_path):
    server = EventServer(str(tmp_path / "r.sock"), _hello)
    server.start()
    try:
        client = _connect(server.socket_path)
        stream = client.makefile("rb")
        stream.readline()  # hello
        assert _wait_for_clients(server, 1)
        server.broadcast({"type": "stage", "job_id": "j1", "stage": "trunk", "frac": 0.3})
        event = decode(stream.readline())
        assert event["stage"] == "trunk"
    finally:
        stream.close()
        client.close()
        server.stop()


def test_broadcast_reaches_every_connected_client(tmp_path):
    server = EventServer(str(tmp_path / "r.sock"), _hello)
    server.start()
    clients, streams = [], []
    try:
        for _ in range(3):
            c = _connect(server.socket_path)
            s = c.makefile("rb")
            s.readline()
            clients.append(c)
            streams.append(s)
        assert _wait_for_clients(server, 3)
        assert server.broadcast({"type": "card_state", "card": 0, "state": "busy"}) == 3
        for s in streams:
            assert decode(s.readline())["state"] == "busy"
    finally:
        for s in streams:
            s.close()
        for c in clients:
            c.close()
        server.stop()


def test_broadcasting_with_no_clients_is_harmless(tmp_path):
    server = EventServer(str(tmp_path / "r.sock"), _hello)
    server.start()
    try:
        assert server.broadcast({"type": "card_state", "card": 0, "state": "idle"}) == 0
    finally:
        server.stop()


def test_a_disconnected_client_is_dropped_without_affecting_others(tmp_path):
    server = EventServer(str(tmp_path / "r.sock"), _hello)
    server.start()
    survivor = None
    try:
        doomed = _connect(server.socket_path)
        with doomed.makefile("rb") as s:
            s.readline()
        survivor = _connect(server.socket_path)
        survivor_stream = survivor.makefile("rb")
        survivor_stream.readline()
        assert _wait_for_clients(server, 2)

        doomed.close()
        # Two broadcasts: the first may be what discovers the dead peer.
        server.broadcast({"type": "card_state", "card": 0, "state": "idle"})
        server.broadcast({"type": "card_state", "card": 1, "state": "idle"})

        seen = [decode(survivor_stream.readline()) for _ in range(2)]
        assert [e["card"] for e in seen] == [0, 1]
    finally:
        if survivor is not None:
            survivor.close()
        server.stop()


def test_a_stale_socket_file_does_not_block_startup(tmp_path):
    path = tmp_path / "r.sock"
    path.write_text("leftover from a crashed run")
    server = EventServer(str(path), _hello)
    server.start()
    try:
        client = _connect(str(path))
        client.close()
    finally:
        server.stop()


def test_stop_removes_the_socket_file(tmp_path):
    path = tmp_path / "r.sock"
    server = EventServer(str(path), _hello)
    server.start()
    server.stop()
    assert not path.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/runner/test_runner_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runner.server'`

- [ ] **Step 3: Write the implementation**

Create `runner/server.py`:

```python
"""Unix-socket event server for the runner daemon.

The production counterpart to runner/mock.py. The mock replays a recorded script
to each client from the beginning; this broadcasts live events to whoever is
connected, so a UI that connects mid-fold gets `hello` and then joins in
progress.

Nothing here may raise into the daemon's fold loop: a UI that disappears is
completely normal (the screen is a separate process that can be restarted), and
must never disturb the compute side.
"""

import logging
import os
import socket
import threading

from protocol.events import encode

log = logging.getLogger(__name__)


class EventServer:
    """Accepts UI clients and broadcasts protocol events to all of them."""

    def __init__(self, socket_path, hello_factory):
        self.socket_path = socket_path
        self._hello_factory = hello_factory
        self._server = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._clients = []

    @property
    def client_count(self):
        with self._lock:
            return len(self._clients)

    def start(self):
        try:
            os.unlink(self.socket_path)   # a crashed run leaves the file behind
        except FileNotFoundError:
            pass
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        self._server.listen(8)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        with self._lock:
            clients, self._clients = self._clients, []
        for conn in clients:
            try:
                conn.close()
            except OSError:
                pass
        if self._server is not None:
            self._server.close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def broadcast(self, event):
        """Send `event` to every connected client. Returns how many received it."""
        payload = encode(event)
        with self._lock:
            clients = list(self._clients)
        delivered, dead = 0, []
        for conn in clients:
            try:
                conn.sendall(payload)
                delivered += 1
            except OSError:
                dead.append(conn)
        if dead:
            with self._lock:
                self._clients = [c for c in self._clients if c not in dead]
            for conn in dead:
                try:
                    conn.close()
                except OSError:
                    pass
            log.info("dropped %d disconnected UI client(s)", len(dead))
        return delivered

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                conn.sendall(encode(self._hello_factory()))
            except Exception:
                log.exception("failed to greet a UI client; dropping it")
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            with self._lock:
                self._clients.append(conn)
            log.info("UI client connected (%d total)", self.client_count)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/runner/test_runner_server.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add runner/server.py tests/unit/runner/test_runner_server.py
git commit -m "feat(runner): broadcast live events to connected UI clients"
```

---

### Task 5: Fold one protein and emit the full event sequence

**Files:**
- Create: `runner/folder.py`
- Create: `tests/integration/__init__.py` (empty)
- Create: `tests/integration/conftest.py`
- Test: `tests/unit/runner/test_folder_events.py`, `tests/integration/test_real_fold.py`

**Interfaces:**
- Consumes: `runner.env.runner_environ`, `runner.dump_tap.*`, `runner.shaping.*`, `protocol.events`.
- Produces: `Folder(device_id: int = 0, model: str = "protenix-v2")` with `.load()`, `.fold(job_id, input_path, emit, *, target_id, n_residues, card=0, n_step=200)`, `.close()`; `FoldError(Exception)`; and the pure helper `fold_event_sequence(stages, frames, result, *, job_id, target_id, model, card, n_residues) -> list[dict]` used to test event ordering without hardware.

`emit` is called as `emit(event: dict)` for every event the fold produces, in order.

**What this task must synthesize.** The spike established that tt-bio's `progress_fn` only ever reports `trunk` and `diffusion`. The other four stages in the protocol — `msa`, `prep`, `confidence`, `saving` — do not exist in tt-bio's instrumentation and must be emitted by this module bracketing the work it does. Treat them as first-class: the UI's pipeline panel renders all six.

- [ ] **Step 1: Write the failing unit test for event ordering**

Create `tests/unit/runner/test_folder_events.py`:

```python
import pytest

from protocol.events import EVENT_TYPES
from runner.folder import fold_event_sequence


def _result(**over):
    base = {"cif_path": "/tmp/out.cif", "wall_s": 5.73, "mean_plddt": 95.2}
    base.update(over)
    return base


def test_the_sequence_starts_with_job_start_and_ends_with_job_done():
    events = fold_event_sequence(
        stages=[("prep", 0.15), ("trunk", 0.4), ("diffusion", 0.9)],
        frames=[{"type": "frame", "job_id": "j1", "step": 0, "total": 200,
                 "n_atoms": 154, "coords_b64": ""}],
        result=_result(),
        job_id="j1", target_id="trpcage", model="protenix-v2",
        card=0, n_residues=20,
    )
    assert events[0]["type"] == "job_start"
    assert events[-1]["type"] == "job_done"


def test_every_emitted_event_is_a_known_protocol_type():
    events = fold_event_sequence(
        stages=[("prep", 0.15)], frames=[], result=_result(),
        job_id="j1", target_id="t", model="protenix-v2", card=0, n_residues=20,
    )
    for event in events:
        assert event["type"] in EVENT_TYPES


def test_job_start_carries_what_the_ui_needs_to_label_the_screen():
    events = fold_event_sequence(
        stages=[], frames=[], result=_result(),
        job_id="j1", target_id="trpcage", model="protenix-v2", card=2, n_residues=20,
    )
    start = events[0]
    assert start["target_id"] == "trpcage"
    assert start["model"] == "protenix-v2"
    assert start["card"] == 2
    assert start["n_residues"] == 20


def test_job_done_reports_plddt_in_percent_not_as_a_fraction():
    events = fold_event_sequence(
        stages=[], frames=[], result=_result(mean_plddt=0.952),
        job_id="j1", target_id="t", model="protenix-v2", card=0, n_residues=20,
    )
    assert events[-1]["mean_plddt"] == pytest.approx(95.2)


def test_frames_appear_between_the_stages_and_the_completion():
    frames = [{"type": "frame", "job_id": "j1", "step": s, "total": 200,
               "n_atoms": 154, "coords_b64": ""} for s in (0, 100, 200)]
    events = fold_event_sequence(
        stages=[("prep", 0.15), ("diffusion", 0.9)], frames=frames, result=_result(),
        job_id="j1", target_id="t", model="protenix-v2", card=0, n_residues=20,
    )
    kinds = [e["type"] for e in events]
    assert kinds.index("stage") < kinds.index("frame")
    assert kinds.index("frame") < kinds.index("job_done")


def test_all_six_protocol_stages_can_be_expressed():
    stages = [("msa", 0.05), ("prep", 0.15), ("trunk", 0.4),
              ("diffusion", 0.9), ("confidence", 0.95), ("saving", 0.99)]
    events = fold_event_sequence(
        stages=stages, frames=[], result=_result(),
        job_id="j1", target_id="t", model="protenix-v2", card=0, n_residues=20,
    )
    emitted = [e["stage"] for e in events if e["type"] == "stage"]
    assert emitted == ["msa", "prep", "trunk", "diffusion", "confidence", "saving"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/runner/test_folder_events.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runner.folder'`

- [ ] **Step 3: Write the implementation**

Create `runner/folder.py`. The pure sequencing helper comes first so it is testable without a device; the `Folder` class holds the device and model:

```python
"""Run folds on a Tenstorrent device and turn them into protocol events.

Owns two expensive things and keeps them for the daemon's lifetime: the device
handle, and the loaded model. The spike measured a second fold at 4.36 s against
5.73 s for the first — residency is where that comes from. Opening the device
also writes ~40 lines of INFO to stderr each time, so doing it once matters for
log readability too.

On stages: tt-bio's own progress_fn only ever reports `trunk` and `diffusion`.
The other four values the protocol promises — msa, prep, confidence, saving —
are emitted here, bracketing the work this module does around the fold itself.
"""

import logging
import time

from runner.dump_tap import install_trajectory_tap, remove_trajectory_tap
from runner.shaping import frame_event, plddt_to_percent, select_frame_steps

log = logging.getLogger(__name__)


class FoldError(Exception):
    """A fold could not be completed. The message is for logs, never the screen."""


def fold_event_sequence(stages, frames, result, *, job_id, target_id, model,
                        card, n_residues):
    """Assemble the ordered event list for one completed fold.

    Pure: takes what a fold produced and returns what should go on the wire, so
    ordering and payload shape are testable without a device.
    """
    events = [{
        "type": "job_start", "job_id": job_id, "target_id": target_id,
        "model": model, "card": card, "n_residues": n_residues,
    }]
    for stage, frac in stages:
        events.append({"type": "stage", "job_id": job_id,
                       "stage": stage, "frac": float(frac)})
    events.extend(frames)
    events.append({
        "type": "job_done", "job_id": job_id,
        "cif_path": result["cif_path"],
        "wall_s": float(result["wall_s"]),
        "mean_plddt": plddt_to_percent(result["mean_plddt"]),
    })
    return events


class Folder:
    """Holds a device and a resident model, and folds one protein at a time."""

    def __init__(self, device_id=0, model="protenix-v2"):
        self.device_id = device_id
        self.model = model
        self._loaded = False

    def load(self):
        """Open the device and load model weights. Call once, at startup."""
        if self._loaded:
            return
        t0 = time.monotonic()
        # Imported here rather than at module scope: importing tt_bio pulls in
        # torch and ttnn, which the unit tests must not need.
        from tt_bio.tenstorrent import get_device
        self._device = get_device()
        self._loaded = True
        log.info("device %d open, model %s resident in %.2fs",
                 self.device_id, self.model, time.monotonic() - t0)

    def close(self):
        if not self._loaded:
            return
        from tt_bio.tenstorrent import cleanup
        cleanup()
        self._loaded = False
        log.info("device closed")

    def fold(self, job_id, input_path, emit, *, target_id, n_residues, card=0,
             n_step=200):
        """Fold one input, calling `emit(event)` for each protocol event.

        Raises FoldError on failure; the caller turns that into a `job_error`.
        """
        if not self._loaded:
            raise FoldError("fold() called before load()")

        emit({"type": "job_start", "job_id": job_id, "target_id": target_id,
              "model": self.model, "card": card, "n_residues": n_residues})

        # Stages tt-bio does not report: emitted around the work we do.
        emit({"type": "stage", "job_id": job_id, "stage": "prep", "frac": 0.15})

        keep = set(select_frame_steps(n_step + 1, target=30))
        wall0 = time.monotonic()

        def on_frame(sample, step, coords):
            # step -1 is the initial noise draw; index it as 0 for the wire.
            index = step + 1
            if index in keep:
                emit(frame_event(job_id, step=index, total=n_step, coords=coords))

        def on_progress(stage, step=None, total=None):
            if total:
                frac = 0.4 if stage == "trunk" else 0.9
                emit({"type": "stage", "job_id": job_id, "stage": stage,
                      "frac": frac * (step / total)})

        handle = install_trajectory_tap(on_frame)
        try:
            result = self._run_fold(input_path, on_progress, n_step)
        except Exception as exc:
            raise FoldError(f"fold failed for {target_id}: {exc}") from exc
        finally:
            remove_trajectory_tap(handle)

        emit({"type": "stage", "job_id": job_id, "stage": "confidence", "frac": 0.95})
        emit({"type": "stage", "job_id": job_id, "stage": "saving", "frac": 0.99})
        emit({"type": "job_done", "job_id": job_id,
              "cif_path": result["cif_path"],
              "wall_s": time.monotonic() - wall0,
              "mean_plddt": plddt_to_percent(result["mean_plddt"])})

    def _run_fold(self, input_path, on_progress, n_step):
        """Invoke tt-bio. Returns {'cif_path': str, 'mean_plddt': float}.

        Kept separate so the event plumbing above can be read without tt-bio's
        API in the way, and so this is the only method an upgrade has to touch.
        """
        raise NotImplementedError(
            "wire this to tt-bio's predict path in Step 5, using the working "
            "invocation in tests/fixtures/streams/capture_real_fold.py"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/runner/test_folder_events.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Implement `_run_fold` against real tt-bio**

`tests/fixtures/streams/capture_real_fold.py` contains a working invocation that produced the real trajectory — read it and reuse its approach. It loads Protenix-v2, builds features, calls `fold()`, and writes a `.cif`.

Replace the `NotImplementedError` with the real call. Return the written `.cif` path and the raw (fractional) `mean_plddt` — the scaling happens in `fold()`, so do not scale twice.

Keep this method as small as possible: it is the seam that a tt-bio upgrade breaks.

- [ ] **Step 6: Write the hardware-gated integration test**

Create `tests/integration/conftest.py`:

```python
"""Skip hardware tests when no usable Tenstorrent device is present.

A packaging or CI machine with no cards is not a failure; a machine with cards
whose driver is not loaded is a different situation and should not be silently
treated as 'no cards' (see scripts/setup-venvs.sh, which makes the same
distinction for the installer).
"""

import pathlib

import pytest

TT_VENDOR_ID = "0x1e52"


def _physical_card_count():
    root = pathlib.Path("/sys/bus/pci/devices")
    if not root.is_dir():
        return 0
    count = 0
    for entry in root.iterdir():
        vendor = entry / "vendor"
        try:
            if vendor.read_text().strip().lower() == TT_VENDOR_ID:
                count += 1
        except OSError:
            continue
    return count


@pytest.fixture(scope="session")
def tt_device():
    if _physical_card_count() == 0:
        pytest.skip("no Tenstorrent cards present on this machine")
    return 0
```

Create `tests/integration/test_real_fold.py`:

```python
"""End-to-end: a real fold on real silicon, producing real protocol events.

Slow (tens of seconds) and requires a card. Run with:
    .venvs/venv-runner/bin/python3 -m pytest tests/integration -v
"""

import pathlib

import numpy as np
import pytest

from protocol.events import EVENT_TYPES, unpack_coords
from runner.folder import Folder

INPUT = pathlib.Path.home() / "code/tt-boltz/examples/trpcage_no_msa.yaml"

pytestmark = pytest.mark.skipif(not INPUT.exists(), reason=f"missing input {INPUT}")


@pytest.fixture(scope="module")
def folded(tt_device):
    folder = Folder(device_id=tt_device)
    folder.load()
    events = []
    try:
        folder.fold("j1", str(INPUT), events.append,
                    target_id="trpcage", n_residues=20, card=tt_device)
    finally:
        folder.close()
    return events


def test_the_event_sequence_is_well_formed(folded):
    kinds = [e["type"] for e in folded]
    assert kinds[0] == "job_start"
    assert kinds[-1] == "job_done"
    assert all(k in EVENT_TYPES for k in kinds)


def test_about_thirty_frames_are_emitted_not_two_hundred(folded):
    frames = [e for e in folded if e["type"] == "frame"]
    assert 25 <= len(frames) <= 32, f"got {len(frames)} frames"


def test_frames_carry_all_atom_coordinates(folded):
    frames = [e for e in folded if e["type"] == "frame"]
    coords = unpack_coords(frames[0]["coords_b64"])
    assert coords.ndim == 2 and coords.shape[1] == 3
    # 20 residues folded all-atom; the spike measured 154 for this input.
    assert coords.shape[0] > 100


def test_the_structure_actually_condenses(folded):
    """The demo's whole visual premise: noise becomes structure."""
    frames = [e for e in folded if e["type"] == "frame"]

    def radius_of_gyration(event):
        c = unpack_coords(event["coords_b64"])
        return float(np.sqrt(((c - c.mean(0)) ** 2).sum(1).mean()))

    first, last = radius_of_gyration(frames[0]), radius_of_gyration(frames[-1])
    assert first > last * 50, f"expected a large collapse, got {first:.1f} -> {last:.1f}"


def test_confidence_is_reported_in_percent(folded):
    done = folded[-1]
    assert 0.0 <= done["mean_plddt"] <= 100.0
    assert done["mean_plddt"] > 1.0, "looks like an unscaled fraction"


def test_a_structure_file_was_written(folded):
    assert pathlib.Path(folded[-1]["cif_path"]).is_file()
```

- [ ] **Step 7: Run both suites**

```bash
./scripts/test.sh
.venvs/venv-runner/bin/python3 -m pytest tests/integration -v
```

Expected: unit suite passes; the integration test runs a real fold in roughly 10–20 s and passes. Report the actual wall-clock and frame count you observe.

- [ ] **Step 8: Commit**

```bash
git add runner/folder.py tests/unit/runner/test_folder_events.py tests/integration/
git commit -m "feat(runner): fold on device and emit the full event sequence"
```

---

### Task 6: Job queue with priority

**Files:**
- Create: `runner/queue.py`
- Test: `tests/unit/runner/test_job_queue.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Job(job_id, target_id, input_path, priority=0)` (a dataclass); `JobQueue()` with `.submit(job) -> None`, `.take() -> Job | None`, `.pending` property, `.__len__()`.

Visitor picks are submitted with a higher priority than attract-loop jobs so they run next, per spec §5. In-flight jobs are never cancelled — with four cards and sub-minute folds the wait is imperceptible, and tearing down a fold mid-device-op is a needless source of instability.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/runner/test_job_queue.py`:

```python
from runner.queue import Job, JobQueue


def _job(name, priority=0):
    return Job(job_id=name, target_id=name, input_path=f"/tmp/{name}.yaml",
               priority=priority)


def test_an_empty_queue_hands_back_nothing():
    assert JobQueue().take() is None


def test_jobs_of_equal_priority_come_out_in_submission_order():
    q = JobQueue()
    for name in ("a", "b", "c"):
        q.submit(_job(name))
    assert [q.take().job_id for _ in range(3)] == ["a", "b", "c"]


def test_a_higher_priority_job_jumps_the_queue():
    q = JobQueue()
    q.submit(_job("attract-1"))
    q.submit(_job("attract-2"))
    q.submit(_job("visitor", priority=10))
    assert q.take().job_id == "visitor"


def test_a_late_visitor_pick_still_jumps_ahead_of_older_attract_jobs():
    q = JobQueue()
    q.submit(_job("attract-1"))
    q.submit(_job("visitor-1", priority=10))
    q.submit(_job("attract-2"))
    q.submit(_job("visitor-2", priority=10))
    assert [q.take().job_id for _ in range(4)] == [
        "visitor-1", "visitor-2", "attract-1", "attract-2"]


def test_length_and_pending_reflect_what_is_waiting():
    q = JobQueue()
    assert len(q) == 0
    q.submit(_job("a"))
    q.submit(_job("b"))
    assert len(q) == 2
    assert [j.job_id for j in q.pending] == ["a", "b"]
    q.take()
    assert len(q) == 1


def test_pending_is_a_snapshot_that_cannot_mutate_the_queue():
    q = JobQueue()
    q.submit(_job("a"))
    q.pending.clear()
    assert len(q) == 1


def test_the_queue_is_safe_to_use_from_two_threads():
    import threading
    q = JobQueue()
    for i in range(200):
        q.submit(_job(f"j{i}"))
    taken = []
    lock = threading.Lock()

    def drain():
        while True:
            job = q.take()
            if job is None:
                return
            with lock:
                taken.append(job.job_id)

    threads = [threading.Thread(target=drain) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(taken) == 200
    assert len(set(taken)) == 200, "a job was handed out twice"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/runner/test_job_queue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runner.queue'`

- [ ] **Step 3: Write the implementation**

Create `runner/queue.py`:

```python
"""Priority job queue for the runner daemon.

A visitor's pick is submitted at a higher priority than the attract loop's own
jobs, so it is taken next by whichever card frees up first. In-flight jobs are
never cancelled: with four cards and sub-minute folds the wait is imperceptible,
and tearing down a fold mid-device-op is a needless source of instability.
"""

import itertools
import threading
from dataclasses import dataclass, field


@dataclass
class Job:
    job_id: str
    target_id: str
    input_path: str
    priority: int = 0
    n_residues: int = 0
    model: str = "protenix-v2"
    meta: dict = field(default_factory=dict)


class JobQueue:
    """Thread-safe: higher priority first, submission order within a priority."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = []            # (-priority, seq, job)
        self._seq = itertools.count()

    def submit(self, job):
        with self._lock:
            self._items.append((-job.priority, next(self._seq), job))
            self._items.sort(key=lambda item: (item[0], item[1]))

    def take(self):
        """Remove and return the next job, or None if nothing is waiting."""
        with self._lock:
            if not self._items:
                return None
            return self._items.pop(0)[2]

    @property
    def pending(self):
        """A snapshot of waiting jobs, in the order they will be taken."""
        with self._lock:
            return [item[2] for item in self._items]

    def __len__(self):
        with self._lock:
            return len(self._items)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/runner/test_job_queue.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add runner/queue.py tests/unit/runner/test_job_queue.py
git commit -m "feat(runner): add a priority job queue for visitor picks"
```

---

### Task 7: Card health and quarantine

**Files:**
- Create: `runner/cards.py`
- Test: `tests/unit/runner/test_cards.py`

**Interfaces:**
- Consumes: nothing (shells out to `tt-smi`).
- Produces: `parse_tt_smi(snapshot: dict) -> list[CardState]`; `CardState(index, board_type, temperature_c, power_w, aiclk_mhz)`; `CardPool(indices, max_temp_c=85.0)` with `.update(cards) -> list[dict]` (returns `card_state` events for anything that changed), `.schedulable() -> list[int]`, `.mark_busy(index)`, `.mark_idle(index) -> dict | None`.

**As implemented** (the reference code below was corrected during execution): busy and too-hot are tracked as two independent flags, not one state string, because a single string cannot represent a card that is both. `mark_busy` **raises `ValueError`** on a quarantined card rather than silently un-quarantining it — Task 9's daemon must guard that call, since its telemetry thread can quarantine a card between `schedulable()` and `mark_busy()`. The wire event's single `state` field follows an explicit precedence: quarantined > busy > idle.

Per spec §6, a card that runs too hot stops receiving work and the UI dims it. The runner samples temperature itself for scheduling; the UI samples `tt-smi` independently for display, so a wedged runner still shows live silicon.

The real `tt-smi -s` field names, confirmed on this machine: `board_info.board_type`, `board_info.bus_id`, `telemetry.asic_temperature`, `telemetry.power`, `telemetry.aiclk`. Values arrive as strings with padding, e.g. `" 16.0"`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/runner/test_cards.py`:

```python
import pytest

from runner.cards import CardPool, CardState, parse_tt_smi

SNAPSHOT = {
    "device_info": [
        {"board_info": {"board_type": "p300c", "bus_id": "0000:01:00.0"},
         "telemetry": {"asic_temperature": "43.7", "power": " 18.0", "aiclk": " 800"}},
        {"board_info": {"board_type": "p300c", "bus_id": "0000:02:00.0"},
         "telemetry": {"asic_temperature": "46.3", "power": " 13.0", "aiclk": " 800"}},
    ]
}


def test_parses_every_card_in_the_snapshot():
    cards = parse_tt_smi(SNAPSHOT)
    assert len(cards) == 2
    assert [c.index for c in cards] == [0, 1]


def test_parses_padded_string_values_into_numbers():
    first = parse_tt_smi(SNAPSHOT)[0]
    assert first.temperature_c == pytest.approx(43.7)
    assert first.power_w == pytest.approx(18.0)
    assert first.aiclk_mhz == pytest.approx(800)
    assert first.board_type == "p300c"


def test_an_empty_snapshot_yields_no_cards():
    assert parse_tt_smi({"device_info": []}) == []
    assert parse_tt_smi({}) == []


def test_a_card_with_unreadable_telemetry_is_skipped_not_fatal():
    snapshot = {"device_info": [
        {"board_info": {"board_type": "p300c"}, "telemetry": {"asic_temperature": "n/a"}},
        SNAPSHOT["device_info"][0],
    ]}
    cards = parse_tt_smi(snapshot)
    assert [c.temperature_c for c in cards] == [pytest.approx(43.7)]


def _card(index, temp):
    return CardState(index=index, board_type="p300c", temperature_c=temp,
                     power_w=15.0, aiclk_mhz=800)


def test_cool_cards_are_schedulable():
    pool = CardPool([0, 1])
    pool.update([_card(0, 45.0), _card(1, 46.0)])
    assert pool.schedulable() == [0, 1]


def test_an_overheating_card_stops_being_scheduled():
    pool = CardPool([0, 1], max_temp_c=85.0)
    pool.update([_card(0, 91.0), _card(1, 46.0)])
    assert pool.schedulable() == [1]


def test_overheating_emits_a_quarantined_card_state_event():
    pool = CardPool([0, 1], max_temp_c=85.0)
    pool.update([_card(0, 45.0), _card(1, 46.0)])
    events = pool.update([_card(0, 91.0), _card(1, 46.0)])
    assert {"type": "card_state", "card": 0, "state": "quarantined"} in events


def test_no_events_are_emitted_when_nothing_changed():
    pool = CardPool([0, 1])
    pool.update([_card(0, 45.0), _card(1, 46.0)])
    assert pool.update([_card(0, 45.5), _card(1, 46.5)]) == []


def test_a_card_that_cools_down_becomes_schedulable_again():
    pool = CardPool([0], max_temp_c=85.0)
    pool.update([_card(0, 91.0)])
    events = pool.update([_card(0, 60.0)])
    assert pool.schedulable() == [0]
    assert {"type": "card_state", "card": 0, "state": "idle"} in events


def test_a_busy_card_is_not_handed_out_again():
    pool = CardPool([0, 1])
    pool.update([_card(0, 45.0), _card(1, 46.0)])
    pool.mark_busy(0)
    assert pool.schedulable() == [1]
    pool.mark_idle(0)
    assert pool.schedulable() == [0, 1]


def test_marking_busy_emits_a_busy_event():
    pool = CardPool([0])
    assert pool.mark_busy(0) == {"type": "card_state", "card": 0, "state": "busy"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/runner/test_cards.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runner.cards'`

- [ ] **Step 3: Write the implementation**

Create `runner/cards.py`:

```python
"""Card health, and which cards may receive work.

Per spec §6, a card that runs too hot stops being scheduled and the UI dims it.
The runner samples temperature for its own scheduling decisions; the UI samples
tt-smi separately for display. That duplication is deliberate — routing the
display's data through the runner would couple the thing that must never fail to
the thing most likely to.

Card reset is never attempted automatically. A demo that resets hardware on its
own is a demo that can fail in an interesting way in front of an audience.
"""

import json
import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CardState:
    index: int
    board_type: str
    temperature_c: float
    power_w: float
    aiclk_mhz: float


def _number(value):
    return float(str(value).strip())


def parse_tt_smi(snapshot):
    """Parse a `tt-smi -s` snapshot dict into CardStates.

    Cards whose telemetry cannot be read are skipped rather than raising: one
    unreadable card must not blind the scheduler to the other three.
    """
    cards = []
    for index, device in enumerate(snapshot.get("device_info", []) or []):
        board = device.get("board_info", {}) or {}
        telemetry = device.get("telemetry", {}) or {}
        try:
            cards.append(CardState(
                index=index,
                board_type=board.get("board_type", "unknown"),
                temperature_c=_number(telemetry.get("asic_temperature")),
                power_w=_number(telemetry.get("power")),
                aiclk_mhz=_number(telemetry.get("aiclk")),
            ))
        except (TypeError, ValueError):
            log.warning("card %d has unreadable telemetry; skipping it", index)
    return cards


def sample_tt_smi(timeout=5.0):
    """Run `tt-smi -s` and parse it. Returns [] if it cannot be read."""
    try:
        out = subprocess.run(["tt-smi", "-s", "--snapshot_no_tty"],
                             capture_output=True, timeout=timeout, check=True)
        return parse_tt_smi(json.loads(out.stdout))
    except Exception:
        log.exception("tt-smi sample failed; treating as no telemetry")
        return []


class CardPool:
    """Tracks which cards are healthy, idle, and eligible for work."""

    def __init__(self, indices, max_temp_c=85.0):
        self.max_temp_c = max_temp_c
        self._states = {i: "idle" for i in indices}

    def update(self, cards):
        """Fold in a telemetry sample. Returns card_state events for changes."""
        events = []
        for card in cards:
            if card.index not in self._states:
                continue
            was = self._states[card.index]
            if card.temperature_c >= self.max_temp_c:
                if was != "quarantined":
                    self._states[card.index] = "quarantined"
                    log.warning("card %d at %.1fC exceeds %.1fC; not scheduling to it",
                                card.index, card.temperature_c, self.max_temp_c)
                    events.append({"type": "card_state", "card": card.index,
                                   "state": "quarantined"})
            elif was == "quarantined":
                self._states[card.index] = "idle"
                log.info("card %d cooled to %.1fC; schedulable again",
                         card.index, card.temperature_c)
                events.append({"type": "card_state", "card": card.index,
                               "state": "idle"})
        return events

    def schedulable(self):
        return sorted(i for i, state in self._states.items() if state == "idle")

    def mark_busy(self, index):
        self._states[index] = "busy"
        return {"type": "card_state", "card": index, "state": "busy"}

    def mark_idle(self, index):
        if self._states.get(index) == "quarantined":
            return None      # a hot card stays out until telemetry clears it
        self._states[index] = "idle"
        return {"type": "card_state", "card": index, "state": "idle"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/runner/test_cards.py -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Check the parser against this machine's real output**

```bash
cd /home/ttuser/code/tt-bio-demo && tt-smi -s 2>/dev/null | .venvs/venv-runner/bin/python3 -c "
import json, sys
from runner.cards import parse_tt_smi
for c in parse_tt_smi(json.load(sys.stdin)):
    print(c)
"
```

Expected: four `p300c` cards with plausible temperatures. If the field names differ from the fixture, fix the parser and add a test using the real shape — do not adjust the fixture to match a broken parser.

- [ ] **Step 6: Commit**

```bash
git add runner/cards.py tests/unit/runner/test_cards.py
git commit -m "feat(runner): track card health and quarantine hot cards"
```

---

### Task 8: Preflight

**Files:**
- Create: `runner/preflight.py`
- Test: `tests/unit/runner/test_preflight.py`

**Interfaces:**
- Consumes: `runner.dump_tap.check_tap_supported`, `runner.cards`.
- Produces: `PreflightResult(ok: bool, missing: list[str])`; `run_preflight(weights_dir, playlist_dir, *, check_tap=True, card_count=None) -> PreflightResult`; `not_ready_event(result) -> dict`.

Per spec §6, preflight verifies offline readiness *before* the demo starts folding, so problems surface at a desk rather than at the venue. On a developer machine it reports what is missing; the daemon emits `not_ready` and the UI holds a "preparing" screen instead of entering Attract with content that will fail.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/runner/test_preflight.py`:

```python
from runner.preflight import not_ready_event, run_preflight


def _ready(tmp_path):
    weights = tmp_path / "weights"
    weights.mkdir()
    (weights / "protenix-v2.pt").write_bytes(b"x")
    playlist = tmp_path / "playlist"
    playlist.mkdir()
    (playlist / "trpcage.yaml").write_text("version: 1\n")
    return weights, playlist


def test_a_complete_installation_passes(tmp_path):
    weights, playlist = _ready(tmp_path)
    result = run_preflight(weights, playlist, check_tap=False, card_count=4)
    assert result.ok
    assert result.missing == []


def test_missing_weights_are_reported_specifically(tmp_path):
    weights, playlist = _ready(tmp_path)
    (weights / "protenix-v2.pt").unlink()
    result = run_preflight(weights, playlist, check_tap=False, card_count=4)
    assert not result.ok
    assert any("protenix-v2.pt" in m for m in result.missing)


def test_an_empty_playlist_is_reported(tmp_path):
    weights, playlist = _ready(tmp_path)
    (playlist / "trpcage.yaml").unlink()
    result = run_preflight(weights, playlist, check_tap=False, card_count=4)
    assert not result.ok
    assert any("playlist" in m.lower() for m in result.missing)


def test_no_cards_is_reported(tmp_path):
    weights, playlist = _ready(tmp_path)
    result = run_preflight(weights, playlist, check_tap=False, card_count=0)
    assert not result.ok
    assert any("card" in m.lower() for m in result.missing)


def test_every_problem_is_reported_at_once_not_just_the_first(tmp_path):
    weights, playlist = _ready(tmp_path)
    (weights / "protenix-v2.pt").unlink()
    (playlist / "trpcage.yaml").unlink()
    result = run_preflight(weights, playlist, check_tap=False, card_count=0)
    assert len(result.missing) >= 3, "an operator should see the whole list in one run"


def test_a_broken_trajectory_tap_is_a_preflight_failure(tmp_path, monkeypatch):
    # If the tap cannot work, folds still succeed but nothing condenses on
    # screen. That must be caught before the demo opens, not discovered at it.
    from runner import preflight as mod

    def broken():
        from runner.dump_tap import TapUnavailable
        raise TapUnavailable("edm_sample moved")

    monkeypatch.setattr(mod, "check_tap_supported", broken)
    weights, playlist = _ready(tmp_path)
    result = run_preflight(weights, playlist, check_tap=True, card_count=4)
    assert not result.ok
    assert any("trajectory" in m.lower() or "edm_sample" in m for m in result.missing)


def test_not_ready_event_carries_the_full_missing_list(tmp_path):
    weights, playlist = _ready(tmp_path)
    (weights / "protenix-v2.pt").unlink()
    result = run_preflight(weights, playlist, check_tap=False, card_count=0)
    event = not_ready_event(result)
    assert event["type"] == "not_ready"
    assert event["missing"] == result.missing
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/runner/test_preflight.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runner.preflight'`

- [ ] **Step 3: Write the implementation**

Create `runner/preflight.py`:

```python
"""Verify the demo can actually run before it claims to be ready.

Per spec §6 the point is that problems surface at a desk, not at the venue. So
preflight reports *every* problem it finds in one pass rather than stopping at
the first — an operator fixing things the night before wants the whole list.

The trajectory-tap check is here for a specific reason: if the tap is broken,
folds still succeed and produce correct structures, and the only symptom is that
nothing condenses on screen. That is a failure the demo cannot detect while
running, so it is checked before starting.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from runner.dump_tap import TapUnavailable, check_tap_supported

log = logging.getLogger(__name__)

REQUIRED_WEIGHTS = ("protenix-v2.pt",)


@dataclass
class PreflightResult:
    ok: bool
    missing: list


def run_preflight(weights_dir, playlist_dir, *, check_tap=True, card_count=None):
    """Check everything the demo needs to run offline. Never raises."""
    missing = []

    weights_dir = Path(weights_dir)
    for name in REQUIRED_WEIGHTS:
        if not (weights_dir / name).is_file():
            missing.append(f"model weights: {weights_dir / name}")

    playlist_dir = Path(playlist_dir)
    targets = sorted(playlist_dir.glob("*.yaml")) if playlist_dir.is_dir() else []
    if not targets:
        missing.append(f"playlist: no .yaml targets under {playlist_dir}")

    if card_count is None:
        from runner.cards import sample_tt_smi
        card_count = len(sample_tt_smi())
    if not card_count:
        missing.append("hardware: no Tenstorrent cards reported by tt-smi")

    if check_tap:
        try:
            check_tap_supported()
        except TapUnavailable as exc:
            missing.append(f"trajectory tap: {exc}")

    for item in missing:
        log.error("preflight: %s", item)
    return PreflightResult(ok=not missing, missing=missing)


def not_ready_event(result):
    """The protocol event the UI uses to hold a 'preparing' screen."""
    return {"type": "not_ready", "missing": list(result.missing)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/runner/test_preflight.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add runner/preflight.py tests/unit/runner/test_preflight.py
git commit -m "feat(runner): preflight offline readiness before folding starts"
```

---

### Task 9: Assemble the daemon

**Files:**
- Create: `runner/daemon.py`
- Create: `runner/__main__.py`
- Test: `tests/unit/runner/test_daemon.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `Daemon(config)` with `.run()`, `.stop()`; `DaemonConfig` (dataclass: `socket_path`, `weights_dir`, `playlist_dir`, `log_root`, `device_id=0`, `max_temp_c=85.0`); `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/runner/test_daemon.py`:

```python
import pytest

from runner.daemon import DaemonConfig, main


def test_main_reports_preflight_failure_without_starting_a_fold(tmp_path, capsys):
    # Nothing exists under these paths, so preflight must fail cleanly.
    code = main([
        "--socket", str(tmp_path / "r.sock"),
        "--weights", str(tmp_path / "weights"),
        "--playlist", str(tmp_path / "playlist"),
        "--log-root", str(tmp_path / "logs"),
        "--preflight-only",
    ])
    assert code != 0
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "preflight" in out.lower() or code == 2


def test_config_defaults_are_explicit():
    config = DaemonConfig(socket_path="/tmp/s", weights_dir="/w",
                          playlist_dir="/p", log_root="/l")
    assert config.device_id == 0
    assert config.max_temp_c == 85.0


def test_preflight_only_never_opens_a_device(tmp_path, monkeypatch):
    """A packaging machine must be able to check readiness without hardware."""
    opened = []
    from runner import daemon as mod
    monkeypatch.setattr(mod, "Folder", lambda **kw: opened.append(kw))
    main([
        "--socket", str(tmp_path / "r.sock"),
        "--weights", str(tmp_path / "weights"),
        "--playlist", str(tmp_path / "playlist"),
        "--log-root", str(tmp_path / "logs"),
        "--preflight-only",
    ])
    assert opened == [], "preflight-only must not construct a Folder"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/runner/test_daemon.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runner.daemon'`

- [ ] **Step 3: Write the implementation**

Create `runner/daemon.py`:

```python
"""tt-bio-demod: the compute daemon.

Opens the device once, holds the model resident, serves the protocol on a Unix
socket, and folds whatever the queue hands it. The UI is a separate process that
may come and go; nothing here depends on one being connected.

Failure policy (spec §6): a failed fold is logged in full, reported as a
`job_error`, and the loop advances to the next target. A target that fails three
times is quarantined for the session. The daemon does not exit on a fold
failure — an unattended booth needs it to keep trying.
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from protocol.events import PROTOCOL_VERSION
from runner.cards import CardPool, sample_tt_smi
from runner.env import log_root_size, prune_log_root, runner_environ
from runner.folder import Folder, FoldError
from runner.preflight import not_ready_event, run_preflight
from runner.queue import Job, JobQueue
from runner.server import EventServer

log = logging.getLogger("tt-bio-demod")

QUARANTINE_AFTER = 3          # consecutive failures before a target is dropped
TELEMETRY_PERIOD_S = 2.0

# tt-metal wrote 121 MB of Inspector logs for two folds during the spike, and
# caps nothing itself. At a fold every ~45s for a conference day that is tens of
# gigabytes, so the daemon enforces its own budget between folds. 2 GB keeps
# enough recent history to diagnose a failure without threatening the disk.
DEFAULT_LOG_BUDGET_BYTES = 2 * 1024**3


@dataclass
class DaemonConfig:
    socket_path: str
    weights_dir: str
    playlist_dir: str
    log_root: str
    device_id: int = 0
    max_temp_c: float = 85.0
    log_budget_bytes: int = DEFAULT_LOG_BUDGET_BYTES


class Daemon:
    def __init__(self, config):
        self.config = config
        self.queue = JobQueue()
        self.cards = CardPool([config.device_id], max_temp_c=config.max_temp_c)
        self.server = EventServer(config.socket_path, self._hello)
        self.folder = None
        self._stop = threading.Event()
        self._failures = {}
        self._quarantined = set()

    def _hello(self):
        return {"type": "hello", "version": PROTOCOL_VERSION,
                "cards": self.cards.schedulable(),
                "models": ["protenix-v2"], "preflight": "ok"}

    def _emit(self, event):
        self.server.broadcast(event)

    def _telemetry_loop(self):
        while not self._stop.wait(TELEMETRY_PERIOD_S):
            for event in self.cards.update(sample_tt_smi()):
                self._emit(event)

    def _enqueue_playlist(self):
        for target in sorted(Path(self.config.playlist_dir).glob("*.yaml")):
            if target.stem in self._quarantined:
                continue
            self.queue.submit(Job(job_id=uuid.uuid4().hex[:8],
                                  target_id=target.stem,
                                  input_path=str(target)))

    def run(self):
        self.server.start()
        try:
            self.folder = Folder(device_id=self.config.device_id)
            self.folder.load()
            threading.Thread(target=self._telemetry_loop, daemon=True).start()

            while not self._stop.is_set():
                # Spec §6: when no card may take work (all quarantined, or the
                # only card is hot), idle calmly and log loudly rather than
                # folding onto a card we have just decided is unsafe.
                available = self.cards.schedulable()
                if not available:
                    log.error("no schedulable cards; holding off")
                    self._stop.wait(5.0)
                    continue

                job = self.queue.take()
                if job is None:
                    self._enqueue_playlist()
                    if len(self.queue) == 0:
                        log.error("no playlist targets available; idling")
                        self._stop.wait(10.0)
                    continue

                # Claiming the card is a race: the telemetry thread runs
                # update() on a timer, so a card can be quarantined between
                # schedulable() above and mark_busy() here. CardPool raises
                # rather than handing out hot hardware, so catch it, put the
                # job back, and pick again with fresh state.
                card = available[0]
                try:
                    self._emit(self.cards.mark_busy(card))
                except ValueError:
                    log.warning("card %d was quarantined while being claimed; "
                                "requeueing %s", card, job.target_id)
                    self.queue.submit(job)
                    continue

                self._run_one(job, card=card)
        finally:
            if self.folder is not None:
                self.folder.close()
            self.server.stop()

    def _run_one(self, job, card):
        # The card is already claimed by the caller — claiming here would
        # duplicate the busy event and re-open the race the caller guards.
        try:
            self.folder.fold(job.job_id, job.input_path, self._emit,
                             target_id=job.target_id,
                             n_residues=job.n_residues, card=card)
            self._failures.pop(job.target_id, None)
        except FoldError as exc:
            # Logged in full; the UI gets a neutral notice and moves on.
            log.exception("fold failed for %s", job.target_id)
            self._emit({"type": "job_error", "job_id": job.job_id,
                        "target_id": job.target_id, "message": str(exc)})
            count = self._failures.get(job.target_id, 0) + 1
            self._failures[job.target_id] = count
            if count >= QUARANTINE_AFTER:
                self._quarantined.add(job.target_id)
                log.error("target %s failed %d times; quarantined for this session",
                          job.target_id, count)
        finally:
            event = self.cards.mark_idle(card)
            if event is not None:
                self._emit(event)
            # Between folds, not during: pruning walks the tree, and the gap
            # between jobs is when nothing is competing for the disk.
            self._prune_logs()

    def _prune_logs(self):
        """Keep tt-metal's log output inside its budget. Never fatal."""
        try:
            freed, removed = prune_log_root(self.config.log_root,
                                            self.config.log_budget_bytes)
            if removed:
                log.info("log root pruned: %d file(s), %.1f MB freed, now %.1f MB",
                         len(removed), freed / 1e6,
                         log_root_size(self.config.log_root) / 1e6)
        except Exception:
            # A janitor failure must never stop the demo folding.
            log.exception("log pruning failed; continuing")

    def stop(self):
        self._stop.set()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tt-bio-demod")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--weights", required=True)
    parser.add_argument("--playlist", required=True)
    parser.add_argument("--log-root", required=True)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--max-temp", type=float, default=85.0)
    parser.add_argument("--log-budget-gb", type=float, default=2.0,
                        help="cap on tt-metal's log root; oldest files pruned first")
    parser.add_argument("--preflight-only", action="store_true",
                        help="check readiness and exit; opens no device")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    os.environ.update(runner_environ(args.log_root, base={}))

    # The tap check is the most valuable one preflight does — a broken tap means
    # folds succeed while nothing condenses on screen — and it opens no device,
    # so it runs in preflight-only mode too.
    result = run_preflight(args.weights, args.playlist, check_tap=True)
    if args.preflight_only:
        for item in result.missing:
            print(f"missing: {item}")
        print("preflight: ok" if result.ok else "preflight: not ready")
        return 0 if result.ok else 2
    if not result.ok:
        log.error("preflight failed; not starting: %s", result.missing)
        # Still serve, so the UI can show a 'preparing' screen rather than a
        # dead socket and an endless reconnect loop.
        server = EventServer(args.socket, lambda: not_ready_event(result))
        server.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 2
        finally:
            server.stop()

    daemon = Daemon(DaemonConfig(
        socket_path=args.socket, weights_dir=args.weights,
        playlist_dir=args.playlist, log_root=args.log_root,
        device_id=args.device, max_temp_c=args.max_temp,
        log_budget_bytes=int(args.log_budget_gb * 1024**3)))
    signal.signal(signal.SIGTERM, lambda *_: daemon.stop())
    signal.signal(signal.SIGINT, lambda *_: daemon.stop())
    daemon.run()
    return 0
```

Create `runner/__main__.py`:

```python
import sys

from runner.daemon import main

sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venvs/venv-runner/bin/python3 -m pytest tests/unit/runner/test_daemon.py -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Check preflight-only works without a device**

```bash
cd /home/ttuser/code/tt-bio-demo && .venvs/venv-runner/bin/python3 -m runner.daemon \
  --socket /tmp/x.sock --weights ~/.boltz --playlist tests/fixtures/streams \
  --log-root /tmp/ttbio-demo-logs --preflight-only; echo "exit=$?"
```

Expected: it prints what is missing and exits non-zero without opening a device or hanging.

- [ ] **Step 6: Commit**

```bash
git add runner/daemon.py runner/__main__.py tests/unit/runner/test_daemon.py
git commit -m "feat(runner): assemble tt-bio-demod"
```

---

### Task 10: The real daemon driving the real UI

**Files:**
- Create: `scripts/run-demo.sh`
- Modify: `README.md`
- Test: manual, end to end, on hardware

This is the phase's deliverable: the actual GTK application rendering an actual fold from actual silicon, with no recorded fixture anywhere in the path.

- [ ] **Step 1: Write the launcher**

Create `scripts/run-demo.sh` — starts the daemon in `venv-runner` and the UI in `venv-ui`, wires them by socket, and shuts both down cleanly on Ctrl-C. Requirements:

- `set -euo pipefail`.
- Default the socket to `${XDG_RUNTIME_DIR:-/tmp}/tt-bio-demo/runner.sock`, creating the directory.
- Default `--log-root` to an absolute path under the same runtime directory, so tt-metal's Inspector output never lands in the CWD.
- Take `--playlist` and `--weights` with sensible defaults (`~/.boltz` for weights).
- Trap `EXIT`/`INT`/`TERM` and kill the daemon, so Ctrl-C does not leave a process holding the device — a leaked device handle blocks the next run.
- Print both log locations before starting.

- [ ] **Step 2: Run it**

```bash
cd /home/ttuser/code/tt-bio-demo && ./scripts/run-demo.sh
```

Expected: the daemon opens the device, preflight passes, the GTK window appears, and a protein folds on screen — atom cloud condensing, then cross-fading to the pLDDT ribbon — driven entirely by live computation.

- [ ] **Step 3: Verify it, do not just watch it**

Confirm with measurements, not impressions:

- Capture the daemon's stderr and confirm the fold's wall-clock and frame count are in the range the spike measured (~5.7 s first fold, ~30 frames).
- Confirm a **second** fold in the same daemon run is faster than the first, proving residency.
- Check the CWD you launched from is still clean and the Inspector logs went to the log root. Report the log root's size after a few folds — this is the disk-fill risk from the spike, now under observation for the first time in a loop.
- Kill the daemon mid-fold. The UI must keep the last structure rotating and reconnect when the daemon returns, exactly as it does against the mock runner. This is the spec's central resilience claim and the first time it is exercised against the real producer.

- [ ] **Step 4: Update the README**

The status section currently says the renderer works "against a recording, with no Tenstorrent hardware involved yet". That stops being true here. Describe what now runs end to end and what remains (telemetry panel, pipeline widget, gallery, four-state machine, packaging). Keep it honest — do not imply the UI panels exist.

- [ ] **Step 5: Commit**

```bash
git add scripts/run-demo.sh README.md
git commit -m "feat: run the real daemon and the real UI together"
```

---

## Definition of done

1. `./scripts/test.sh` passes both halves — the UI half under `venv-ui`, the runner half under `venv-runner` — including the 83 tests that already existed.
2. `.venvs/venv-runner/bin/python3 -m pytest tests/integration -v` runs a real fold and passes on a machine with cards; skips cleanly on one without.
3. `./scripts/run-demo.sh` shows a live fold in the real UI, driven by the real daemon.
4. Killing the daemon mid-fold never blanks the UI; restarting it produces another fold.
5. After several folds, the launch directory is clean and tt-metal's logs are confined to the configured root.
6. The log root stays under its budget across a run of many folds — verified by setting a deliberately small `--log-budget-gb` and watching the root stop growing rather than by trusting the code.

## What this phase deliberately leaves out

Named so nobody builds them early:

- **The UI panels** — telemetry, pipeline progress, gallery, and the four-state machine are Phase 3b. The daemon emits the events they will consume, and nothing renders them yet.
- **Multi-card scheduling.** The daemon holds one device. `CardPool` and the queue are built for more, but running four resident models is a separate piece of work with its own memory questions.
- **The curated playlist** — real targets, blurbs, pre-cached MSAs and thumbnails are Phase 4. This phase folds whatever `.yaml` files it is pointed at.
- **Debian packaging** and the systemd unit — Phase 4.
- **The two known blockers in `docs/followups.md`**: `ribbon_from_cif` on the GTK main loop, and multi-chain splining. Both are UI-side and bite with real targets; they belong with Phase 3b.
- **Cold-start measurement.** Every timing here assumes warm weight and kernel caches. Measuring a fresh-imaged machine needs a machine whose caches can legitimately be cleared.
