# The two venvs: what they are, why they're separate, how to build them

tt-bio-demo is two processes in two Python environments (see
[the design spec](superpowers/specs/2026-08-10-tt-bio-demo-design.md), §2, and
`CLAUDE.md`). This project owns both of those environments itself, via
`scripts/setup-venvs.sh` — there is nothing left to install by hand and
nothing borrowed from a personal environment on the box.

## The two environments

```
<prefix>/
  venv-ui/       from /usr/bin/python3, WITH --system-site-packages
  venv-runner/   from /usr/bin/python3, WITHOUT system site packages
```

**`venv-ui`** runs the GTK4 UI process (`ui/`). It's built from the *system*
`/usr/bin/python3` with `--system-site-packages`, so it inherits whatever apt
already installed for the system interpreter: `python3-gi` (GObject
introspection / GTK4 bindings), `python3-gemmi` (structural biology / CIF
parsing), `python3-opengl` (PyOpenGL), `python3-numpy`. These are all painful
to get right via `pip` because they're thin wrappers around system libraries —
GObject introspection data, the system GTK4, the system OpenGL/GL stack — that
apt already manages correctly and consistently with the rest of the desktop.
Reinstalling them from PyPI risks a second, slightly different copy of
libraries the desktop compositor also links against. `--system-site-packages`
sidesteps that entirely: nothing to reinstall, nothing to get out of sync.

**`venv-runner`** runs the compute daemon (`runner/`, built out in Phase 3). It's
isolated — no `--system-site-packages` — because `pip install tt-bio` pulls in
torch and ttnn, and those have no business sharing a site-packages with
PyGObject. Isolation also buys fault isolation for free: the compute side can
hang, leak device memory, or die on a wedged card, and none of that touches a
process that never imported it.

Both venvs are built from the same `/usr/bin/python3` — never from a personal
Tenstorrent venv on `$PATH`. See "the bare-`python3` trap" below for why that
distinction is load-bearing on this box specifically.

### Why two venvs instead of the single one the design spec sketched

The original design spec (§2) drew the boundary as "system `python3` +
`python3-gi`" on one side and "`/opt/tt-bio-demo/venv`" on the other — i.e. only
the *runner* side got a venv, and the UI ran against the bare system
interpreter. That's left as-is in the spec (it's a historical record of a
decision, not a live document), but this bootstrap script refines it: the UI
side gets its own venv too, rather than running bare. The reasoning the spec
gave — GTK and tt-bio's stack must never share an environment — is unchanged.
What changes is that "bare system `python3`" isn't reproducible: it depends on
whatever happens to be on `$PATH` and what apt happened to install for the
system interpreter at the time. A `--system-site-packages` venv gets the same
apt-provided packages but is a named, recreatable artifact instead of an
ambient fact about the machine — which is what "the installer can reproduce it
on a fresh QB2" requires.

## Building and activating

```bash
scripts/setup-venvs.sh                    # builds .venvs/venv-ui and .venvs/venv-runner
scripts/setup-venvs.sh --skip-runner       # UI only — skips the slow pip install
scripts/setup-venvs.sh --force             # rebuild both from scratch
scripts/setup-venvs.sh --strict            # a degraded venv-runner (see below) becomes fatal
scripts/setup-venvs.sh --prefix /opt/tt-bio-demo   # what the Debian postinst will pass, later
```

The script is idempotent: re-running it with both venvs already valid is a
close-to-free no-op (it re-verifies both by actually importing, then exits).
`--force` throws away and rebuilds; without it, a venv with the wrong (or
missing) tt-bio version is rebuilt automatically, but a venv with the *right*
version already installed is left alone even if nothing else changed.

### Exit codes

Automation (a Debian postinst, CI, a person's shell script) needs to tell
"fully working" apart from "installed but unusable" without parsing this
script's text output, so the exit code carries that distinction:

| Code | Meaning |
|---|---|
| `0` | Everything requested was built/verified and works. |
| `1` | A hard failure: bad preconditions, missing apt packages, `venv-ui` failed verification, `venv-runner`'s `pip install` itself failed, an `rm -rf` couldn't remove an existing venv (see "permissions" below), or — with `--strict` — `venv-runner`'s stack failed to import. |
| `2` | Soft/degraded: `venv-runner` has the pinned `tt-bio` version installed, but its `torch`/`ttnn`/`tt_bio` stack does not import. Reported, not fatal, unless `--strict` is given. |

Exit `2` is expected, not a bug, on a box where `tt-bio install-deps` hasn't
been run — see the "what actually happens on install" section below for why
that's routine pre-Phase-3. `--strict` exists for a caller (a later,
hardware-ready Phase 3 postinst, say) that needs a working runner and should
treat "installed but degraded" the same as "failed outright."

It also checks apt prerequisites up front — `python3-venv`, `python3-pip`,
`python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-4.0`, `python3-gemmi`,
`python3-opengl`, `python3-numpy`, `libgl1`, `libglu1-mesa` — and prints the
exact `apt install` line if anything is missing, rather than failing partway
through venv creation with a confusing traceback. On the dev box these were
all already present.

**The tt-bio version is pinned in exactly one place**: the `TT_BIO_VERSION`
variable at the top of `scripts/setup-venvs.sh`. Bump it deliberately, per the
project convention of pinning to a release tag rather than tracking `main`.

Activation, once built:

```bash
source .venvs/venv-ui/bin/activate        # or just call .venvs/venv-ui/bin/python3 directly
source .venvs/venv-runner/bin/activate
```

Day to day, you shouldn't need to activate anything — use `scripts/test.sh` for
the suite and `.venvs/venv-ui/bin/python3 -m ui.app` to run the app.

### Running the suite

```bash
scripts/test.sh              # full suite through venv-ui
scripts/test.sh -k geometry -v   # args forwarded straight to pytest
```

This is now the one obvious way to run tests — it resolves `venv-ui` itself
(default `.venvs/venv-ui` next to the repo; override with `TT_BIO_DEMO_PREFIX`
if you built somewhere else) and fails with a clear message if it isn't built
yet, instead of quietly running under whatever `python3` happens to resolve to.

## The bare-`python3` trap (still real, no longer something to remember)

A personal Tenstorrent virtualenv (`~/.tenstorrent-venv`, uv-managed) is active
on `$PATH` ahead of `/usr/bin` on the dev box. It's a genuinely different
CPython *build* from the system interpreter — 3.12.12 vs. apt's 3.12.3 — and it
has numpy but not gemmi, PyGObject, or PyOpenGL, because those come from apt
and only install for the system interpreter. Before this script existed, the
rule was "always type `/usr/bin/python3` explicitly, never bare `python3`" (see
`docs/followups.md`), and it cost a task to discover the hard way: tests passed
by accident on the numpy-only modules and failed obscurely the moment gemmi or
GTK was touched.

That trap is still there — the personal venv is still on `$PATH`, bare
`python3` still resolves to it — but there's no longer anything to remember,
because `scripts/setup-venvs.sh` and `scripts/test.sh` never invoke bare
`python3` at all; they always go through an explicit venv path. The rule moves
from "hold this fact in your head every time you type a command" to "use the
project's scripts and you get it for free." If you ever do need to run
something by hand, use `.venvs/venv-ui/bin/python3` (or
`.venvs/venv-runner/bin/python3` for tt-bio work) — not bare `python3`, and not
`/usr/bin/python3` either, since going through the venv also gets you the
right site-packages, not just the right interpreter.

## `venv-runner` / `tt-bio` — what actually happens on install

`pip install tt-bio==0.6.2` (from PyPI, requires-python `>=3.10,<3.13,!=3.11.*`
— system Python is 3.12.3, so it qualifies) pulls in a large dependency tree:
torch, `ttnn==0.68.0` (also on PyPI), rdkit, pandas, transformers, biotite,
matplotlib, scikit-learn, and a long tail of smaller packages — including a
full set of `nvidia-*` CUDA runtime wheels, because PyPI's `torch` wheel
declares them unconditionally regardless of whether an NVIDIA GPU is present.
None of that is Tenstorrent-specific; it's just what `torch`'s default PyPI
wheel drags in.

**Measured on this box:** first cold install (nothing cached), **46 s**, venv
**6.0 GB** on disk. A second from-scratch build with pip's wheel cache warm
took 33 s for the same 6.0 GB — the network transfer, not disk or CPU, is the
bottleneck, and this box has fast enough bandwidth that "multiple GB, likely
slow" from the original brief turned out to be multiple GB but not especially
slow. Expect this to vary a lot with the network at the actual provisioning
site; budget for it being much slower on a fresh QB2 at a venue with worse
connectivity, and for the on-disk size to stay roughly the same regardless.

### What "verified" actually checks — and a false-positive it used to miss

The first version of this script verified `venv-runner` with a bare `import
tt_bio` and called that "verified." That check is too shallow to trust:
`tt_bio`'s top-level `__init__.py` is deliberately lazy — it does nothing but
read its own installed version via `importlib.metadata` — so `import tt_bio`
succeeds even in a venv where `torch` was never installed or got wiped out
partway through. Confirmed empirically, not assumed: diffing `sys.modules`
before/after a bare `import tt_bio` in this exact `venv-runner` shows it pulls
in nothing but stdlib. The real work, and the real `import torch, ttnn`, lives
one level down — in modules like `tenstorrent.py`, `protenix.py`, `boltz2.py`,
`worker.py` — none of which a bare top-level import ever touches.

This was caught by simulating an interrupted install: copy `venv-runner`,
delete its `torch/` tree and `torch-*.dist-info`, and re-run the (old)
verification against the copy. It printed "already valid, skipping" and
exited `0`, while `import torch` in that same venv raised
`ModuleNotFoundError`. That is the exact "half-built environment reports
success" failure this script exists to prevent — it just wasn't checking
deep enough to notice its own runner venv was in that state.

`verify_runner_venv` (in `scripts/setup-venvs.sh`) now checks four things
individually, so a failure names the actual missing/broken piece rather than
just "tt_bio didn't import":

1. `import torch`
2. `import ttnn`
3. `import tt_bio`
4. `import tt_bio.tenstorrent` — the shared Tenstorrent-compute primitives
   module every model family (`boltz2`, `protenix`, `opendde`, ...) is built
   on, per their own docstrings. A clean import of it is a real smoke test
   that tt_bio's own code loads against the `torch`/`ttnn` actually present,
   not just that the lazy top-level package ran without complaint.

Re-running the same torch-deletion scenario against the fixed check correctly
reports the degraded venv (see "Verification performed" below) instead of a
false "already valid."

**What this deliberately does *not* check:** opening a Tenstorrent device, or
anything that needs `tt-bio install-deps`'s system libraries/kernel modules.
`import ttnn` alone does not touch hardware — verified: it imports cleanly on
this box with no device opened, producing only noisy `ttnn`/tt-metal debug
logging and some harmless nanobind reference-counting warnings at interpreter
shutdown (`LOGURU_LEVEL`/`TT_METAL_LOGGER_LEVEL` are set before the import,
matching tt-bio's own `main.py`, to keep that quiet). Whether tt-bio can
actually *drive* hardware is a Phase-3 question; this script only answers
whether its Python stack imports.

Other tt-bio dependencies (`gemmi`, `rdkit`, `pandas`, `transformers`,
`biotite`, ...) are declared requirements and pip did install them, but they
are not individually checked here — they're much smaller downloads than
`torch`/`ttnn` and dramatically less likely to be the specific casualty of an
install interrupted partway through (pip installs the heaviest packages last
in the dependency-resolved order on this box, so a truncated run leaves
`torch`/`ttnn`/their `nvidia-*` wheels as the prime suspects, not e.g.
`click`). If experience says otherwise on a different box or under a
different failure mode, widen the check — it should track what actually goes
missing, not a guess made once and left unrevisited.

**`import tt_bio`, `torch`, `ttnn`, and `tt_bio.tenstorrent` all succeed after
a plain `pip install`** on this box — no Tenstorrent system libraries or
kernel modules were required just to import them. That's a meaningfully
different outcome from "pip succeeds but the import fails without system
deps," which is the other failure mode this script watches for. It does
**not** mean the runner is ready to do real work: `tt-bio install-deps`
(Tenstorrent system packages, kernel modules, `tt-metal`/`tt-smi` userspace)
has deliberately never been run by this script or anywhere else in this repo
— that's an explicit-consent, Debian-packaging-phase decision, not a
venv-bootstrap one. Whether the import continues to succeed *and* whether
tt-bio can actually drive hardware without those system deps installed are
two different questions; only the first has been verified here. Phase 3,
which will actually invoke tt-bio against hardware, is where the second gets
answered.

If a future run of this script reports `pip install` succeeding but the
import check failing (exit `2`, or exit `1` with `--strict`), that is the
signal that this box's assumption (no system deps needed for the import)
doesn't hold everywhere — treat it as exactly that signal, not as a bug in
the script. `verify_runner_venv` prints the precise `ExceptionType: message`
from the failing import, naming which of the four checks failed, for this
reason.

## Idempotency and the bug it caught

Re-running `scripts/setup-venvs.sh` against an already-valid pair of venvs
is close to free (well under a second): it re-verifies both by actually
importing the relevant packages rather than trusting a marker file, then
prints the summary and exits. `--force` discards and rebuilds unconditionally;
without it, `venv-runner` is only rebuilt if the installed `tt-bio` version
doesn't match `TT_BIO_VERSION`.

Building that idempotency check surfaced a genuine bug worth remembering: the
first version of the "what version of tt-bio is already installed" check
piped `pip show tt-bio` into `awk '/^Version:/{print $2; exit}'` inside a
plain variable assignment. `awk`'s early `exit` closes its end of the pipe
before `pip show` has finished writing the rest of its output, so `pip` gets
`SIGPIPE` and exits nonzero; with `pipefail` set, that makes the whole pipeline
nonzero; with `set -e`, that kills the script outright — on a plain assignment,
with no error message at all. The fix was to capture `pip show`'s full output
into a variable first and parse it with no live pipe underneath, avoiding the
race entirely. Any `producer | consumer_with_early_exit` pipeline under
`set -e -o pipefail` has this bug latent in it; it just happens not to fire on
the first (cold) run, only once there's something for `pip show` to actually
report — which is exactly when idempotency gets exercised for real. As a
precaution, every `du -sh ... | cut -f1` size computation elsewhere in the
script (same plain-assignment-with-live-pipe shape) got an `|| true` for the
same reason, even though `du -s` producing exactly one line doesn't trigger
the SIGPIPE race the way `awk`'s early `exit` did — cheap insurance against
the same class of bug, not a fix for an observed second instance of it.

**Permissions.** `rm -rf` on an existing venv (for `--force`, or an automatic
rebuild on a version mismatch) goes through a `safe_rm_rf` helper that
captures `rm`'s stderr and turns a failure into a named `die()` instead of
bash's raw, context-free "Permission denied" under `set -e`. This matters in
practice once `--prefix /opt/tt-bio-demo` is in play: a non-root user
re-running this script against a venv a root-owned postinst created would
otherwise get killed by `set -e` with no explanation of why or what to do
about it.

## Verification performed

- `scripts/setup-venvs.sh` from a clean `.venvs/`: both venvs created,
  `venv-ui` verifies `gi`+`Gtk 4.0`, `gemmi`, `OpenGL`, `numpy`; `venv-runner`
  verifies `torch`, `ttnn`, `tt_bio`, and `tt_bio.tenstorrent` individually.
  Exit 0.
- Re-run immediately after: both venvs detected as already valid, no rebuild,
  exit 0, close to a second (importing `torch`+`ttnn`+`tt_bio.tenstorrent`
  every idempotent run costs a little more than the original bare-`tt_bio`
  check did, in exchange for that check meaning something).
- `--force`: both venvs discarded and rebuilt from scratch (pip's wheel cache
  warm, so the reinstall was ~35s); exit 0.
- The false-positive repro, against a scratch copy (not the real 6 GB
  `venv-runner` — a copy, deleted afterward): copied `venv-runner`, removed
  its `torch/` tree and `torch-*.dist-info`, ran the script with
  `--prefix` pointed at the copy. Before the fix this reported "already
  valid, skipping" and exited `0`. After the fix it reports `VERIFY-FAIL:
  torch import failed: ModuleNotFoundError: No module named 'torch'`, sets
  `venv-runner`'s status to the degraded state, and exits `2`. The same
  scenario with `--strict` added dies outright with exit `1`.
- `safe_rm_rf` against a directory made unwritable to simulate a permissions
  failure: dies with a named remedy (exit `1`) instead of a raw, unexplained
  `rm: Permission denied` under `set -e`.
- `scripts/test.sh` (i.e. the 83-test suite through `venv-ui`): all pass.
- `.venvs/venv-ui/bin/python3 -m ui.app` under `WAYLAND_DISPLAY=wayland-0
  DISPLAY=:0`: window opens, no stack trace, only a benign
  `Gtk-WARNING: Unknown key gtk-modules in .../gtk-4.0/settings.ini` (pre-existing,
  unrelated to this change). No process left running afterward.
