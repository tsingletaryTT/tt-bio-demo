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
| `0` | Everything requested was built/verified and works — including, on a box with Tenstorrent cards attached, a real device open/close. |
| `1` | A hard failure: bad preconditions, missing apt packages, `venv-ui` failed verification, `venv-runner`'s `pip install` or SFPI download/hash-verify itself failed, an `rm -rf` couldn't remove an existing venv (see "permissions" below), or — with `--strict` — `venv-runner`'s import/device check failed. |
| `2` | Soft/degraded: `venv-runner` has the pinned `tt-bio` version and a matching SFPI installed, but its `torch`/`ttnn`/`tt_bio` stack doesn't import, or a device probe on hardware that's actually present fails. Reported, not fatal, unless `--strict` is given. |

Exit `2` is expected, not a bug, on a box with no Tenstorrent cards attached
at all (a packaging/CI machine, say) or one still missing the Tenstorrent
driver/kernel module — see "the SFPI toolchain" and "the device probe" below.
`--strict` exists for a caller (a later, hardware-ready Phase 3 postinst,
say) that needs a working runner and should treat "installed but degraded"
the same as "failed outright."

It also checks apt prerequisites up front — `python3-venv`, `python3-pip`,
`python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-4.0`, `python3-gemmi`,
`python3-opengl`, `python3-numpy`, `libgl1`, `libglu1-mesa`, `curl`,
`xz-utils` (the last two for downloading and extracting SFPI, below) — and
prints the exact `apt install` line if anything is missing, rather than
failing partway through venv creation with a confusing traceback. On the dev
box these were all already present.

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

**Measured on this box:** first cold `pip install` (nothing cached), ~46 s;
warm-cache reinstalls since, ~33-45 s — the network transfer, not disk or
CPU, is the bottleneck, and this box has fast enough bandwidth that
"multiple GB, likely slow" from the original brief turned out to be multiple
GB but not especially slow. Expect this to vary a lot with the network at the
actual provisioning site; budget for it being much slower on a fresh QB2 at a
venue with worse connectivity.

SFPI (below) adds its own download+extract on top: ~80 MB, a few seconds,
~435 MB extracted. Total `venv-runner` size is **~6.4 GB** (pip install
alone is ~6.0 GB; SFPI accounts for the rest).

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

`verify_runner_venv` (in `scripts/setup-venvs.sh`) checks these individually,
so a failure names the actual missing/broken piece rather than just
"tt_bio didn't import":

1. `import torch`
2. `import ttnn`
3. `import tt_bio`
4. `import tt_bio.tenstorrent` — the shared Tenstorrent-compute primitives
   module every model family (`boltz2`, `protenix`, `opendde`, ...) is built
   on, per their own docstrings. A clean import of it is a real smoke test
   that tt_bio's own code loads against the `torch`/`ttnn` actually present,
   not just that the lazy top-level package ran without complaint.
5. A real device probe — see "the device probe" below. Added after the four
   import checks above turned out to still miss a second, distinct
   false-positive (a broken SFPI toolchain), not covered by any import.

Re-running the same torch-deletion scenario against the fixed check correctly
reports the degraded venv (see "Verification performed" below) instead of a
false "already valid."

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
kernel modules were required just to import them. Whether tt-bio can
actually *drive* hardware, though, turned out to be a separate question the
import check could not answer at all — see the next two sections.

If a future run of this script reports the import check specifically failing
(as opposed to the device probe — `verify_runner_venv` names which of the
five checks failed), that's the signal to look at whether this box still has
the Tenstorrent driver/kernel module it needs, since SFPI (the other thing
that used to require `tt-bio install-deps`) is now handled below without it.

## The SFPI toolchain: why it's vendored per-venv, not taken from the system

`ttnn` doesn't just import — at first use it JIT-compiles Tenstorrent device
kernels with a RISC-V toolchain called **SFPI**. tt-bio 0.6.2's
`ttnn==0.68.0` requires **SFPI 7.35.3** specifically; this fact lives in
`<ttnn>/tt_metal/sfpi-version`, a small manifest the wheel ships, not
anywhere this script guesses at. This dev box, though, also has a
system-wide SFPI installed at `/opt/tenstorrent/sfpi` for the user's other
Tenstorrent projects — and it's **7.61.0**, a different version.

That skew breaks kernel compilation outright. Confirmed on this box: with
only the system 7.61.0 available, `ttnn.open_device()` throws

```
TT_THROW @ tt_metal/jit_build/build.cpp:60
brisc build failed. Log: lto1: internal compiler error in lto_read_decls
```

— and on at least one run, instead of a catchable exception, the same
mismatch **segfaulted** the Python process outright (`Signal: Segmentation
fault (11)`, inside `libtt_metal.so`'s JIT build path). Both outcomes are
real; which one happens seems to depend on exactly which kernel fails to
build first. Neither is caught by any import check — `torch`, `ttnn`, and
even `tt_bio.tenstorrent` all imported cleanly the entire time. Only actually
opening a device exercises kernel compilation at all.

`ttnn`'s shared objects have two SFPI paths compiled in: the absolute system
path above, and a *relative* `<ttnn>/runtime/sfpi`, which the wheel ships
with nothing inside. Whichever exists wins the relative path if present —
confirmed by dropping SFPI 7.35.3 there and getting a clean
`open_device()`/`close_device()`, then moving that same directory aside and
getting the exact failure above back, with nothing else changed. Unsetting
`TT_METAL_HOME` first and repeating both directions made no difference
either way, which rules out the obvious "maybe it's an environment variable"
explanation — the mechanism really is the relative path on disk.

So `scripts/setup-venvs.sh` gives `venv-runner` its own private SFPI instead
of relying on (or mutating) the system one:

- **Version and hashes come from the wheel's own manifest**, never
  hardcoded. `ensure_sfpi_installed` sources
  `<ttnn>/tt_metal/sfpi-version` — the same file, read the same way,
  tt-metal's own `dockerfile/scripts/install-sfpi.sh` uses — so a future
  `TT_BIO_VERSION` bump that changes `ttnn`'s required SFPI is picked up
  automatically. The host's architecture and distro family are detected with
  the identical algorithm `tenstorrent/sfpi`'s own `sfpi-info.sh` uses (exact
  `uname -m`; distro is `/etc/os-release`'s `ID` unless `ID_LIKE` names a
  `debian` or `fedora` ancestor — Ubuntu's `ID=ubuntu, ID_LIKE=debian`
  resolves to `debian`, matching the manifest's binary matrix).
- **Downloaded from `${sfpi_repo}/releases/download/${version}/...` and
  sha256-verified against the manifest before anything is extracted.** A
  mismatch is a hard failure (`die`, script exits 1) — this is a compiler
  toolchain arriving over the network, and refusing a corrupted or tampered
  download is not optional. Measured on this box: ~80 MB download, a few
  seconds; ~435 MB extracted.
- **Extracted to `<ttnn>/runtime/sfpi`.** The tarball's own top-level entry
  is already `sfpi/`, so it's extracted into `runtime/` (not `runtime/sfpi/`)
  — landing at `runtime/sfpi/...` directly rather than the doubly-nested
  `runtime/sfpi/sfpi/...` a naive `mkdir sfpi && extract into it` would
  produce.
- **Idempotent via a receipt this function writes itself**, not by trusting
  that a `runtime/sfpi` directory existing means it's correct.
  `ensure_sfpi_installed` writes `runtime/sfpi/.tt-bio-demo-sfpi-receipt`
  (`version=... sha256=...`) immediately after a hash-verified install, and
  only trusts an existing directory if that exact receipt is present and
  matches the *current* manifest's version/hash. Anything else — no receipt,
  a stale receipt from a different `ttnn` version, or a directory someone
  placed there by hand — is treated as untrusted and replaced, never assumed
  correct on the strength of merely existing. (This was tested directly: a
  real SFPI 7.35.3 was hand-placed at `runtime/sfpi` during development to
  unblock other testing before this logic existed; once it did, the script
  was run against a state with that directory *removed entirely* to prove
  it rebuilds from nothing, and separately against a directory holding valid
  files but no receipt, which it correctly logged as untrusted and replaced.)

**Why not just run `tt-bio install-deps` and let it fix the system SFPI?**
Because that installs/upgrades the *system* copy at `/opt/tenstorrent/sfpi`
— which would fix this venv but could just as easily break whichever other
Tenstorrent project on this box is pinned to 7.61.0. Vendoring is the same
"give each thing its own environment" principle the rest of this script is
built on (see "why two venvs"), applied one level deeper: instead of two
processes needing two Python environments, it's two *projects* needing two
compiler toolchains, and the fix is the same shape. A consequence worth
calling out explicitly since earlier text here said otherwise: **`tt-bio
install-deps` is no longer needed for SFPI at all**, which removes one
system-mutating step from what a turnkey Debian install has to do. It may
still matter for other Tenstorrent system libraries or kernel modules this
script doesn't touch — see "the device probe" below.

## The device probe: catching what imports can't

Every import check above — `torch`, `ttnn`, `tt_bio`, `tt_bio.tenstorrent` —
passed throughout the SFPI investigation. None of them open a device, so
none of them could have caught it. `verify_runner_venv` now ends with an
actual probe:

1. `ttnn.get_num_devices()` — how many Tenstorrent cards does this process
   see at all.
2. If **zero**, stop there and report success anyway: `device probe SKIPPED
   (0 Tenstorrent devices detected)`. A packaging or CI machine legitimately
   has no cards; that is not an install failure, and treating it as one
   would make this script unusable anywhere but a fully wired QB2.
3. If **one or more**, actually call `ttnn.open_device(device_id=0)` then
   `ttnn.close_device(...)`. A clean pair is the only thing that counts as
   `device probe OK (N device(s) detected, opened+closed device 0)`. Any
   exception, *or the subprocess dying outright* (the segfault case above),
   is a real failure — this box has cards, so "the toolchain can't actually
   compile for them" is exactly the failure this step exists to catch.

That third case is why the probe runs inside the same subprocess-isolated
`verify_runner_venv` every other check already used (a fresh `python3 -`
per call, not the calling shell): a segfault there is just a nonzero exit
status to bash, handled by the same `if verify_runner_venv ...; then` this
script already had — no special crash handling needed, because the process
boundary already provides it. Confirmed directly, not assumed: one of the
sabotage runs during development (SFPI's `compiler/` directory renamed away,
leaving a stale-but-present receipt) triggered the real segfault rather than
a clean exception, and the script still reported the correct degraded state
and exit code.

On this box, with real hardware and a correctly vendored SFPI, the probe
reports `device probe OK (4 device(s) detected, opened+closed device 0)`.

## Idempotency and the bug it caught

Re-running `scripts/setup-venvs.sh` against an already-valid pair of venvs
is close to free (a few seconds, not a full rebuild): it re-verifies both by
actually importing the relevant packages and probing a device rather than
trusting a marker file, then prints the summary and exits. `--force`
discards and rebuilds unconditionally; without it, `venv-runner`'s `pip
install` is only redone if the installed `tt-bio` version doesn't match
`TT_BIO_VERSION` — but `ensure_sfpi_installed` still runs on every pass
regardless (it's a receipt check, not a re-download, so it's cheap), which
means an existing venv whose SFPI went missing, stale, or untrusted gets
fixed without redoing the multi-GB pip install.

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

- `scripts/setup-venvs.sh` from a clean `.venvs/` (SFPI's own `runtime/sfpi`
  removed entirely first, not just the rest of the venv, to prove SFPI
  installs from nothing rather than inheriting a copy left over from
  development): both venvs created, `venv-ui` verifies `gi`+`Gtk 4.0`,
  `gemmi`, `OpenGL`, `numpy`; `venv-runner` verifies `torch`, `ttnn`,
  `tt_bio`, `tt_bio.tenstorrent`, downloads+hash-verifies+installs SFPI
  7.35.3, and probes a real device: `device probe OK (4 device(s) detected,
  opened+closed device 0)`. Exit 0, ~57s total (SFPI download is small next
  to the pip install).
- Re-run immediately after: both venvs detected as already valid (SFPI via
  its receipt, no re-download), no rebuild, exit 0, ~3s (the device probe
  itself takes real wall-clock time now, unlike a bare import).
- `--force`: both venvs discarded and rebuilt from scratch, including a
  fresh SFPI download+verify+install; exit 0, ~45-60s depending on pip cache
  state, device probe OK again afterward.
- The tt_bio-only false-positive repro (from the prior review round), against
  a scratch copy: copied `venv-runner`, removed its `torch/` tree and
  `torch-*.dist-info`, ran the script with `--prefix` pointed at the copy.
  Reports `VERIFY-FAIL: torch import failed: ModuleNotFoundError: No module
  named 'torch'`, degraded state, exit `2` (exit `1` with `--strict`).
- **The SFPI mechanism itself**, both directions, on the real venv-runner
  (not a copy — SFPI is small enough to rebuild that this was cheap): with
  the vendored `runtime/sfpi` present, `open_device`/`close_device` both
  report `OK`; with it moved aside (system 7.61.0 the only SFPI left),
  `ttnn.open_device()` throws the `TT_THROW @ .../build.cpp:60` / `lto1:
  internal compiler error` failure described above; moving it back restores
  the clean pass. Unsetting `TT_METAL_HOME` first and repeating both
  directions changed nothing, ruling out the environment-variable
  explanation.
- **The genuine segfault**, not just the catchable-exception case: sabotaging
  the vendored SFPI's `compiler/` directory (renamed away, receipt left
  intact) against a scratch copy sometimes produced a clean `RuntimeError`
  and sometimes an actual `Signal: Segmentation fault (11)` inside
  `libtt_metal.so`. In the segfault case, `verify_runner_venv`'s subprocess
  boundary meant the crash surfaced to the calling script as nothing more
  than a nonzero exit status — no special handling needed, and the script
  still reported the correct degraded state and exit code (`2` without
  `--strict`, `1` with it).
- **Hash mismatch refusal**: temporarily corrupted the expected sha256 in a
  copy of `<ttnn>/tt_metal/sfpi-version`, ran the script. It downloaded the
  (correct) tarball, computed its real hash, found it didn't match the
  doctored manifest value, and died with a named mismatch error instead of
  installing anything — restored the manifest afterward.
- **Untrusted-directory replacement**: hand-placed a fake `runtime/sfpi`
  (real subdirectory names, bogus contents, no receipt file). The script
  logged it as untrusted and replaced it with a real, verified install
  rather than assuming its mere presence meant it was fine.
- **No-hardware branch**: `ttnn.get_num_devices() == 0` is not exercisable on
  this box (it has 4 real cards), so the branch was verified by extracting
  `verify_runner_venv`'s exact Python body and running it against a stub
  `ttnn` module whose `get_num_devices()` returns `0` and whose
  `open_device` raises if called at all (to prove the real one is never
  invoked in this branch). Result: `device probe SKIPPED (0 Tenstorrent
  devices detected)`, exit 0 — a no-hardware machine is reported as fully
  working, not degraded.
- `safe_rm_rf` against a directory made unwritable to simulate a permissions
  failure: dies with a named remedy (exit `1`) instead of a raw, unexplained
  `rm: Permission denied` under `set -e`.
- `scripts/test.sh` (i.e. the 83-test suite through `venv-ui`): all pass.
- `.venvs/venv-ui/bin/python3 -m ui.app` under `WAYLAND_DISPLAY=wayland-0
  DISPLAY=:0`: window opens, no stack trace, only a benign
  `Gtk-WARNING: Unknown key gtk-modules in .../gtk-4.0/settings.ini` (pre-existing,
  unrelated to this change). No process left running afterward.
