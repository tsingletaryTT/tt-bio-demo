"""tt-metal environment containment for the runner daemon.

tt-metal's Inspector/Watcher subsystems write structured logs for every kernel they
run, to a `generated/` tree that lands *relative to the process CWD* by default.
Measured during the Phase 3a spike (docs/spike-real-fold.md): two 200-step folds
produced 121 MB under `generated/`. A booth running one fold every ~45 seconds for a
conference day would produce gigabytes and eventually fill the disk, so the daemon
pins the location to an absolute path it owns and can rotate.

Measured behaviour of the relevant variables, Task 1 Step 1 of the Phase 3a plan.
Every probe below is a bare `ttnn.open_device()` + `ttnn.close_device()` (no fold,
no kernels beyond device bring-up) run from a fresh scratch CWD each time, so the
byte counts are a floor, not the 121 MB fold figure -- but they are enough to prove
*where* the bytes land, which is what this module controls.

    baseline (no tt-metal env vars set):
        `generated/` appears in the CWD. 36 KB: `generated/inspector/` holds 5
        yaml files (startup.yaml, mesh_workloads_log.yaml, kernels.yaml,
        programs_log.yaml, mesh_devices_log.yaml); `generated/watcher/` holds 2
        text files (kernel_names.txt, kernel_elf_paths.txt).

    TT_METAL_INSPECTOR_LOG_PATH=<absolute path>  (the name the Phase 3a plan's
    draft code assumed, and the name this module's LOG_ROOT_VAR was originally
    going to be):
        NO EFFECT. `generated/` still landed in the CWD, still 36 KB, byte-for-
        byte identical to the baseline run above, and the target directory was
        never created. `strings` on the installed `ttnn/build/lib/libtt_metal.so`
        (and a recursive grep across the entire venv-runner site-packages tree)
        confirms why: the string `TT_METAL_INSPECTOR_LOG_PATH` does not appear
        anywhere in the binary. It is not a variable this tt-metal build reads at
        all. This is the single most important finding of Step 1: the obvious
        name is not merely misleading (as TT_METAL_WATCHER=0 turned out to be) --
        it is fictional for this build, and building containment on it would have
        shipped Task 1 with zero actual containment.

    TT_METAL_INSPECTOR=0:
        A real but partial effect, and not a location fix. `generated/inspector/`
        disappears entirely (all 5 yaml files gone), but `generated/watcher/`'s 2
        files still land relative to the CWD exactly as in the baseline. Total
        dropped from 36 KB to 16 KB. This silences one of the two subsystems but
        does nothing about *where* output goes, so it does not solve containment
        by itself.

    TT_METAL_LOGS_PATH=<absolute path>  (found by grepping the strings actually
    present in libtt_metal.so once TT_METAL_INSPECTOR_LOG_PATH turned up nothing
    -- not a variable named or suggested anywhere in the Phase 3a plan):
        This is the variable that actually relocates the tree. The entire
        `generated/` directory (both `inspector/` and `watcher/`) moved to the
        given absolute path; the scratch CWD came back completely empty (`ls -A`
        showed nothing). Combined with TT_METAL_INSPECTOR=0, only `watcher/`'s 2
        small files remained, still at the pinned path, CWD still empty, 20 KB
        total for a bare open/close.

    TT_METAL_WATCHER=0: deliberately NOT probed here. The Phase 3a spike already
    paid for this finding once: it hung the box for two minutes in a busy-poll
    ("Watcher checking device N" repeating, not a crash -- had to be force-killed,
    though no lasting damage was found afterward). Do not set it and do not retry
    the probe to double check; the earlier finding is trusted as-is.

Consequence for this module: `LOG_ROOT_VAR` below is bound to `TT_METAL_LOGS_PATH`,
not to `TT_METAL_INSPECTOR_LOG_PATH` as the plan's Step 4 draft literally wrote --
that name is inert on this box and pinning to it would have produced a module that
imports cleanly, passes its own unit tests (which never assert the literal string
value), and still lets the daemon fill the disk from wherever it happens to start.
The public surface (`LOG_ROOT_VAR`, `runner_environ`, `log_root_size`,
`prune_log_root`) is otherwise exactly what the plan specified.

Do not assume these variables mean what their names suggest -- verify each one on
the actual installed build before depending on it, the same way this module's
LOG_ROOT_VAR itself turned out to need correcting.

UPDATE, Task 10 (first sustained multi-fold run in a single long-lived daemon
process): TT_METAL_LOGS_PATH correctly relocates `generated/`, and
runner/daemon.py's prune_log_root sweep correctly deletes the oldest files
under it once the budget is exceeded -- but for one specific file, that sweep
turns out not to free anything real. tt-metal's Inspector opens
`generated/inspector/mesh_workloads_log.yaml` exactly once, at device
bring-up, and holds that file descriptor open and appending for the rest of
the daemon's life -- it is never closed and reopened between folds the way
the earlier (short-lived, two-fold, then-exit) validation runs would have
exercised. Unlinking an open file on Linux removes its name from the
directory but does not free its blocks; the process holding the fd keeps
writing to the now-nameless inode until it closes that fd (i.e. until the
daemon itself exits). The practical effect, measured directly against a
running daemon via `lsof` rather than trusted from the daemon's own
`log_root_size()` (which walks the directory tree and therefore cannot see
an unlinked-but-open file at all): after a prune, `log_root_size()` reports
the log root as ~0 bytes while `lsof -p <daemon-pid>` shows the "deleted"
mesh_workloads_log.yaml still open and growing -- 938 MB to 1.79 GB in the
60 seconds after one such prune, ~13-14 MB/s sustained, entirely invisible
to the metric the daemon logs and trusts. At that rate a conference day
would consume several hundred GB, and since this script's own default log
root lives under $XDG_RUNTIME_DIR (tmpfs, i.e. RAM) rather than persistent
disk, the failure mode is an OOM, not a full disk. The other Inspector/
Watcher files (kernels.yaml, programs_log.yaml, mesh_devices_log.yaml,
watcher/kernel_names.txt, watcher/kernel_elf_paths.txt) were checked the
same way and do NOT reproduce this: their fds stay a constant size across
repeated folds (spot-checked over 20s of continuous folding with no
growth), consistent with them being written once at device/kernel bring-up
rather than appended to per-fold. mesh_workloads_log.yaml is therefore the
only file in this tree with unbounded-while-open growth, and it is also the
one TT_METAL_INSPECTOR=0 removes entirely (per the probe two paragraphs up:
"generated/inspector/ disappears entirely ... watcher/'s 2 files still land
... but the Inspector subsystem specifically is silenced"). Nothing in this
codebase reads generated/inspector/ programmatically, so disabling it costs
nothing here. runner_environ now sets TT_METAL_INSPECTOR=0 by the same
setdefault discipline as LOG_ROOT_VAR, so an operator who deliberately wants
Inspector output for debugging (by setting TT_METAL_INSPECTOR themselves
before launching) keeps that choice. This does not make prune_log_root
pointless -- kernels.yaml/programs_log.yaml/mesh_devices_log.yaml still
accumulate slowly across a long-running daemon's many distinct kernel
compilations and still benefit from the budget sweep -- it removes the one
file class the sweep could not actually touch while the daemon runs.

NOT FREE, though: `strings` on the same libtt_metal.so contains this
verbatim (found while double-checking the fix rather than taking the
"Inspector is safe to disable" conclusion above on faith): "Running
without Inspector logger will impact tt-triage functionality." Nothing in
this project uses tt-triage today, so the trade is made deliberately, but
an operator debugging with that tool elsewhere on the same tt-metal build
needs to know this daemon disables the thing it depends on by default --
set TT_METAL_INSPECTOR=1 before launching (README's "Running it today"
section says the same).
"""

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# tt-metal reads this to decide where Inspector/Watcher write `generated/`.
# Absolute paths only. See the module docstring above: this is TT_METAL_LOGS_PATH,
# not the TT_METAL_INSPECTOR_LOG_PATH the name might suggest -- that variable is
# not read by this tt-metal build at all (verified empirically and via `strings`).
LOG_ROOT_VAR = "TT_METAL_LOGS_PATH"

# Silences tt-metal's Inspector subsystem outright. See the "UPDATE, Task 10"
# section of the module docstring above for why this is on by default: the
# one file Inspector writes that grows without bound for the daemon's entire
# life (generated/inspector/mesh_workloads_log.yaml, opened once at device
# bring-up and never closed until the process exits) cannot be bounded by
# prune_log_root while the daemon keeps running -- unlinking an open file
# does not free it. Nothing in this codebase reads Inspector's output.
INSPECTOR_VAR = "TT_METAL_INSPECTOR"


def runner_environ(log_root, base=None):
    """Return an environment mapping with tt-metal's log output pinned.

    `log_root` may be relative; it is resolved against the current directory so
    the daemon's own CWD can never leak into where gigabytes get written. An
    operator who has already set LOG_ROOT_VAR (or INSPECTOR_VAR) keeps their
    choice -- both are filled in with setdefault, never overwritten.
    """
    env = dict(os.environ if base is None else base)
    env.setdefault(LOG_ROOT_VAR, str(Path(log_root).resolve()))
    env.setdefault(INSPECTOR_VAR, "0")
    return env


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


def prune_log_root(log_root, max_bytes, *, dry_run=False, protect=None):
    """Delete oldest log files until the root fits in `max_bytes`.

    Returns (bytes_freed, paths_removed).

    This function deletes files, so it is deliberately narrow about what it will
    touch: regular files only, never symlinks (so a link inside the root cannot
    be used to reach anything outside it), never the root directory itself, and
    nothing at all if the root is missing or is not a directory. Oldest-first by
    mtime, so the newest logs -- the ones useful for diagnosing whatever just
    happened -- are the last to go.

    `protect`, if given, is an iterable of path strings (matching `str(path)`
    for a file found under `log_root`) that must never be deleted regardless
    of age or budget pressure -- added for runner/daemon.py's structures
    budget (Task 10 review finding): tt-metal's own log files are never read
    back by anything in this codebase once written, but a `.cif` the UI has
    not gotten around to reading yet (dispatched via GLib.idle_add, behind
    whatever else is queued on the GTK main loop) is a real, referenced file,
    and "oldest first" alone has no notion of "still in use." Protected
    files still count toward the total this function is deciding whether to
    prune at all -- so a root can end up parked above `max_bytes` if the
    protected set alone exceeds it. That is a correctness floor (never
    delete something a caller told you not to), not a budget guarantee, and
    is deliberate: the alternative is deleting a file a caller explicitly
    asked to keep.
    """
    root = Path(log_root)
    if not root.is_dir():
        return 0, []
    protect = frozenset(protect) if protect else frozenset()

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
        if str(path) in protect:
            continue
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
    else:
        # OVER BUDGET AND NOTHING WAS PRUNABLE. Without this line the caller's
        # `if removed:` gate makes a misconfigured budget look exactly like a
        # healthy sweep -- same silence, run after run, while the root sits
        # above its limit (docs/followups.md, from Phase 3a). The floor itself
        # is deliberate (never delete a file a caller asked to keep), so this
        # is not an error; it is the one case the metric could not otherwise
        # distinguish from "nothing to do".
        #
        # No condition on this `else`, and that is deliberate. Getting here
        # means the early return above did not fire (so `total > max_bytes`)
        # and nothing was removed -- and `freed` only ever moves with
        # `removed`, so `freed` is 0 and the root IS still over budget. A
        # guard restating that could never be false; it was written that way
        # first, and the mutation that proved it redundant is why it is not.
        log.warning("log root %s holds %.1f MB against a %.1f MB budget and "
                    "every file over it is protected; nothing was pruned",
                    root, total / 1e6, max_bytes / 1e6)
    return freed, removed
