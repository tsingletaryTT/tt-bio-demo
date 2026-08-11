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


def runner_environ(log_root, base=None):
    """Return an environment mapping with tt-metal's log output pinned.

    `log_root` may be relative; it is resolved against the current directory so
    the daemon's own CWD can never leak into where gigabytes get written. An
    operator who has already set LOG_ROOT_VAR keeps their choice.
    """
    env = dict(os.environ if base is None else base)
    env.setdefault(LOG_ROOT_VAR, str(Path(log_root).resolve()))
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


def prune_log_root(log_root, max_bytes, *, dry_run=False):
    """Delete oldest log files until the root fits in `max_bytes`.

    Returns (bytes_freed, paths_removed).

    This function deletes files, so it is deliberately narrow about what it will
    touch: regular files only, never symlinks (so a link inside the root cannot
    be used to reach anything outside it), never the root directory itself, and
    nothing at all if the root is missing or is not a directory. Oldest-first by
    mtime, so the newest logs -- the ones useful for diagnosing whatever just
    happened -- are the last to go.
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
