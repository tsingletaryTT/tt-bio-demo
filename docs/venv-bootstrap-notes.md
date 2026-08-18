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
scripts/setup-venvs.sh --dev               # also install pytest into venv-runner (see below)
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
| `0` | Everything requested was built/verified and works — including, on a box with Tenstorrent cards physically present, a real device open/close. Also `0` when no Tenstorrent PCI hardware is present at all (see "the device probe" below for why that's a distinct, deliberately checked case, not just "`ttnn` reported zero"). |
| `1` | A hard failure: bad preconditions, missing apt packages, `venv-ui` failed verification, `venv-runner`'s `pip install`/SFPI download/hash-verify/extraction itself failed, an `rm -rf` couldn't remove an existing venv (see "permissions" below), or — with `--strict` — `venv-runner`'s import/device check failed. |
| `2` | Soft/degraded: `venv-runner` has the pinned `tt-bio` version and a matching SFPI installed, but its `torch`/`ttnn`/`tt_bio` stack doesn't import, **or** Tenstorrent PCI hardware is physically present but not usable — driver not loaded/bound, a failed `open_device()`, or the probe timing out (a possible wedged card). Reported, not fatal, unless `--strict` is given. |

**Important correction from an earlier draft of this doc:** exit `2` on "the
driver/kernel module is missing" is something this script had to be *made*
true — it was not true by construction, and is exactly the kind of claim this
note says elsewhere not to make without testing it. The first version of the
device probe used `ttnn.get_num_devices() == 0` as its only signal for "no
hardware, skip the probe" — but that call returns an empty list with no
exception whenever `/dev/tenstorrent` doesn't exist, which is exactly the
state of a box with cards installed but no driver loaded. A card-less
packaging machine and a QB2 with an unloaded `tt-kmd` reported the *identical*
zero, and the script reported the identical exit `0` for both — silently
promoting "driver absent" to "no hardware, that's fine" instead of catching
it. See "the device probe" below for the fix and how it was verified without
touching this box's actual driver.

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
scripts/test.sh                  # both halves, one combined verdict
scripts/test.sh -k geometry -v   # args forwarded, unchanged, to both halves
```

This is the one obvious way to run tests, and running it means something
different than it used to: `scripts/test.sh` no longer runs the suite through
`venv-ui` alone. As of Phase 3a, the suite is split across the two venvs —
`tests/unit/runner/` (and `tests/integration/`, once it exists) run *only*
under `venv-runner`; everything else in `tests/unit/` runs *only* under
`venv-ui`. `scripts/test.sh` runs both halves itself, one after the other,
and reports a single combined pass/fail — a developer or CI job invokes the
one script and gets a trustworthy answer for the whole suite, not half of it.
See the "How the split is decided" section in `scripts/test.sh`'s own header
comment for the full reasoning (a directory boundary, not a pytest marker —
a marker turned out not to work here, see below) and
[`infra-test-split-report.md`](superpowers/sdd/2026-08-11-runner-daemon/infra-test-split-report.md)
for the verification transcripts.

It still resolves both venvs itself (default `.venvs/venv-ui` and
`.venvs/venv-runner` next to the repo; override with `TT_BIO_DEMO_PREFIX` if
you built somewhere else) and fails with a clear message if either isn't
built yet — including, for `venv-runner`, pointing at `--dev` specifically if
the venv exists but `pytest` isn't in it (see the next section) — instead of
quietly running under whatever `python3` happens to resolve to, or silently
running zero tests for a half whose venv is missing.

**Adding a test:** if it imports anything from `runner.*` — directly, or via
the module under test — put it in `tests/unit/runner/`, not `tests/unit/`.
Everything else goes in `tests/unit/` directly. Guessing wrong is loud, not
silently skipped: a runner-side file left in `tests/unit/` either explodes the
*entire* UI half's collection (pytest must import every file under its search
path before it can even see markers or run anything, so one `import torch` at
module scope there is fatal to the whole half, not just that file) or — the
sharper trap, since venv-runner *does* have a `gemmi` (a transitive `tt-bio`
dependency, a different version than venv-ui's apt-provided one) — imports
fine on the wrong side and silently gives wrong answers instead of erroring.
Confirmed directly: `tests/unit/test_geometry_load.py` collects and runs
without any import error under `venv-runner`, but two of its assertions
(`test_missing_b_factor_column_raises`, `test_missing_b_factor_value_defaults_to_fifty`)
fail there while passing 10/10 under `venv-ui` — the two `gemmi`s parse the
same fixture differently. "It imported" is therefore never the test for which
half a file belongs on.

A pytest marker (`pytestmark = pytest.mark.runner`) was tried first and
rejected, not just skipped for style: pytest must import a module to read its
markers, and `tests/unit/`'s UI-side modules (`ui/app.py`, `ui/viewer.py`, ...)
hard-fail that import under `venv-runner` with `ModuleNotFoundError: No
module named 'gi'` regardless of any marker elsewhere — which aborts the
*entire* venv-runner collection (pytest's own `Interrupted: N errors during
collection`, exit code `2`) before any marker is ever consulted. A marker
cannot prevent a crash that happens at import time, before markers exist to
the collector at all. Only "which directory is this file in" can, since it
decides whether pytest opens the file in the first place — hence the
directory split, enforced in `scripts/test.sh` via `--ignore=tests/unit/runner`
for the UI half and an explicit path for the runner half.

### Running `venv-runner`'s own tests: `--dev`, and why it's a flag, not automatic

Phase 3a (the runner daemon, `runner/`) adds a second suite of unit tests that
must run *through `venv-runner`*, not `venv-ui` — `tests/unit/runner/test_runner_env.py`
and everything the Phase 3a plan
([`docs/superpowers/plans/2026-08-11-runner-daemon.md`](superpowers/plans/2026-08-11-runner-daemon.md))
adds after it, all specified as `.venvs/venv-runner/bin/python3 -m pytest ...`
(and, since the test-infra split above, all collected automatically by a
plain `scripts/test.sh` too — no separate invocation needed day to day).
`pip install tt-bio` does not pull in `pytest` — it's not one of `tt-bio`'s own
dependencies — so a plain `scripts/setup-venvs.sh` run leaves `venv-runner`
unable to run them at all: `ModuleNotFoundError: No module named 'pytest'`.

This was caught, not designed in advance: Task 1 of Phase 3a needed `pytest` in
`venv-runner` to do its own TDD, found it missing, and `pip install`ed it by
hand outside the script — which passed that one task but left the fix
unrecorded anywhere a rebuild or a `--force` would preserve it. Fixed properly
by adding a `--dev` flag rather than just re-running the manual `pip install`:

```bash
scripts/setup-venvs.sh --dev
```

**Why a flag, and not just installing `pytest` unconditionally:** `venv-runner`
is not only a dev environment — it's the exact artifact the Debian postinst
builds later, at `--prefix /opt/tt-bio-demo`, for a real booth machine. Test
tooling (and `pytest`'s own dependency chain — `pluggy`, `iniconfig`) is dead
weight and extra supply-chain surface there, for zero benefit: nothing on a
booth machine runs `pytest`. That's the same reasoning this script already
applies to `tt-bio install-deps` and the system SFPI (§"why not just run
`tt-bio install-deps`" above) — give production only what it needs, keep dev
conveniences opt-in. So `--dev` installs `pytest` into `venv-runner`; leaving
it off (the default, including whatever a future Debian postinst passes) keeps
the venv exactly as lean as `pip install tt-bio==${TT_BIO_VERSION}` makes it.

Mechanically: `ensure_test_deps_installed` (in `scripts/setup-venvs.sh`) checks
whether `pytest` is importable and reports that in the summary's `test deps:`
line *unconditionally* — so the line stays accurate on a plain re-run after an
earlier `--dev` run already added it — but only actually runs `pip install
pytest` when `--dev` was passed on that invocation. No version pin, unlike
`tt-bio`/SFPI: `pytest` has no coupling to what `venv-runner` actually runs at
a booth (a version mismatch doesn't break kernel compilation the way SFPI skew
does), so "already importable" is a good enough idempotency check without a
receipt file.

**Verified, not just asserted, on a genuinely fresh prefix** (never built
before, not the dev box's existing `.venvs/`):

```bash
scripts/setup-venvs.sh --prefix /tmp/tt-bio-demo-fresh-venvs --dev
```
— full from-scratch build (venv-ui, venv-runner, `pip install tt-bio==0.6.2`,
SFPI download+verify+install, device probe), 55s wall clock (pip's cache was
warm; see "measured on this box" above for why that's expected), summary
reported `test deps: installed (pytest, just added via --dev)`, exit `0`.
The version above is deliberately not bumped with the pin: this is a record of
one measured run against 0.6.2, and rewriting it to say 0.6.3 would attach a
timing to a build nobody has done. It stands until someone re-runs it.
Then, against that exact freshly-built venv:
```bash
/tmp/tt-bio-demo-fresh-venvs/venv-runner/bin/python3 -m pytest tests/unit/test_runner_env.py -v
```
— `15 passed`. A subsequent idempotent re-run with `--dev` reported `test
deps: installed (pytest)` (the plain, no-reinstall wording) rather than
re-running pip. Separately, uninstalling `pytest` from that same venv and
re-running **without** `--dev` reproduced the summary's documented degraded
line, `test deps: not installed (pass --dev to add pytest for Phase 3a's unit
tests)`, and confirmed attempting to run the tests anyway fails with the
plain, immediately-recognizable `No module named pytest` — which, combined
with the summary line already printed by that same build, is the "fail loudly
enough that the reason is obvious" this flag is meant to guarantee rather than
leaving to chance. The temp prefix was removed afterward; the dev box's own
`.venvs/venv-runner` was never touched by any of this and still has `pytest`
9.1.1 (from the original manual install) throughout.

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

`pip install tt-bio==0.6.3` (from PyPI, requires-python `>=3.10,<3.13,!=3.11.*`
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
kernels with a RISC-V toolchain called **SFPI**. tt-bio 0.6.3's
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
- **Downloaded (with a connect/max-time timeout — see "the device probe"'s
  timeout reasoning, the same logic applies to a stalled download) from
  `${sfpi_repo}/releases/download/${version}/...` and sha256-verified
  against the manifest before anything is extracted.** A mismatch is a hard
  failure (`die`, script exits 1) — this is a compiler toolchain arriving
  over the network, and refusing a corrupted or tampered download is not
  optional. Measured on this box: ~80 MB download, a few seconds; ~435 MB
  extracted.
- **Extracted to a staging directory first, never straight into
  `<ttnn>/runtime/sfpi`.** The tarball's own top-level entry is already
  `sfpi/`, so within staging it lands at `staging/sfpi/...` — checked for
  explicitly, to catch a differently-laid-out tarball rather than silently
  producing the doubly-nested `.../sfpi/sfpi/...` a blind extract would.
  Extraction itself is guarded (`if ! tar ...; then die ...; fi`), not left
  to `set -e`'s raw propagation — a partial/failed extraction gets a named
  remedy message, consistent with everything else in this script, rather
  than `tar`'s own exit status and no context.
- **The old directory, if any, is never removed until the replacement is
  fully downloaded, hash-verified, and extracted.** A review round caught
  the original ordering — remove the untrusted old copy, *then* fetch the
  replacement — turning a transient network blip during a routine
  re-verification into `runtime/sfpi` being completely empty, worse than
  the wrong-version state it started in. The fix downloads and verifies to
  a scratch location and a staging extraction directory under
  `runtime/.sfpi-staging.XXXXXX` first; only once that staging copy is
  known-good does the swap happen — the old directory (if present) is
  renamed aside (`runtime/sfpi.old.<pid>`, an O(1) rename, not a recursive
  copy), the staging copy is renamed into `runtime/sfpi`, and only then is
  the `.old` one actually deleted. That shrinks the window in which
  `runtime/sfpi` doesn't exist from "as long as the network takes" to "the
  time between two renames," and even a crash in that narrow window leaves
  the old copy recoverable at its `.old` name rather than gone. Verified
  directly: corrupting the download URL, then the expected hash, then the
  tarball itself (via a local `file://` fixture) all confirmed the
  pre-existing untrusted directory — including a marker file planted in
  it — survived completely untouched, with no leftover staging directory
  either. A successful run afterward confirmed the marker was gone and a
  correct receipt was in place, with no `.old`/`.staging` litter left over.
- **Idempotent via a receipt this function writes itself**, not by trusting
  that a `runtime/sfpi` directory existing means it's correct.
  `ensure_sfpi_installed` writes a receipt (`version=... sha256=...`) inside
  the staging copy before it's ever swapped into place, and only trusts an
  existing directory if that exact receipt is present and matches the
  *current* manifest's version/hash. Anything else — no receipt, a stale
  receipt from a different `ttnn` version, or a directory someone placed
  there by hand — is treated as untrusted and replaced, never assumed
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
none of them could have caught it. `verify_runner_venv` ends with an actual
probe — which itself went through a review round that found the first
version of this probe had exactly the "half-built environment reports
success" problem this whole script exists to prevent, just one layer deeper
than the import checks did.

### Why `ttnn.get_num_devices() == 0` cannot mean "no cards"

The first version of the probe used `ttnn.get_num_devices()` as its only
signal for hardware presence: zero meant "skip the probe, report success."
That's wrong, traced all the way through tt-metal's own source:
`ttnn.get_num_devices()` calls `GetNumAvailableDevices()`, which calls
`Cluster::number_of_user_devices()`, which goes through UMD's
`PCIDevice::enumerate_devices()` — and that function's very first move is
`if (!std::filesystem::exists("/dev/tenstorrent/")) return device_ids;`,
handing back an **empty list, with no exception**. `/dev/tenstorrent/*` is
created by the `tt-kmd` kernel module. So a box with four physical cards
whose driver is unloaded, missing, or failed to bind reports the exact same
`0` as a card-less packaging machine — `get_num_devices()` alone cannot tell
those two states apart, and only one of them is a real failure. That is a
plausible state for this project specifically: a conference-eve QB2 after a
kernel update is exactly the kind of box that might have cards installed and
no bound driver, and the old probe would have called that "no hardware,
fine" and exited `0`.

The fix establishes physical presence independently of the driver, by
counting Tenstorrent PCI devices (vendor ID `0x1e52`) directly via
`/sys/bus/pci/devices/*/vendor`. PCI enumeration happens in the kernel
regardless of whether any driver is bound to a device, unlike
`/dev/tenstorrent`, so it's the ground truth `get_num_devices()` can be
checked against rather than trusted blindly. Combining the two signals gives
three states instead of two:

| PCI devices (vendor `0x1e52`) | `ttnn.get_num_devices()` | Verdict |
|---|---|---|
| 0 | (not checked) | No cards at all — pass, probe skipped |
| >0 | >0 | Cards present and usable — try opening one |
| >0 | 0 | **Cards present, driver absent/unbound — fail** |

Reported, respectively: `device probe SKIPPED (0 Tenstorrent PCI devices
detected)`; `device probe OK (N PCI device(s) present, M usable,
opened+closed device 0)`; or a `VERIFY-FAIL` naming the mismatch explicitly
("N Tenstorrent PCI device(s) present but ttnn.get_num_devices() reports 0
-- the tt-kmd driver is most likely not loaded or not bound"). Only the third
row is new behavior; the first two match what the old probe already did
correctly when the two signals agreed (real cards present and a bound
driver, or genuinely nothing installed).

### If there's a card that's present but can't open

Once cards are confirmed physically present and the driver reports at least
one, the probe actually calls `ttnn.open_device(device_id=0)` then
`ttnn.close_device(...)`. A clean pair is the only thing that counts as `M
usable`. Any exception — or the subprocess dying outright, see the segfault
below — is a real failure: cards are present and the driver sees them, so
"the toolchain can't actually compile for them" (the SFPI story above) is
exactly the failure this step exists to catch.

### The whole probe runs under a timeout

Both failure modes reproduced during this work — the `TT_THROW` and the
segfault — failed fast. Neither exercised a hang. Tenstorrent hardware has a
documented wedged-card state that needs a warm reset, and nothing rules out
a mismatched toolchain triggering that as a hang instead of a crash. In an
unattended postinst, a probe that can hang forever wedges the whole install
with nobody present to notice. The entire probe — imports and device probe
together, one `python3 -` subprocess — runs under
`timeout --kill-after=10s ${DEVICE_PROBE_TIMEOUT_SECONDS}s` (120s by
default: generous next to the couple of seconds the happy path takes, but
bounded). A timeout is reported as `VERIFY-FAIL: device probe timed out
after ${N}s`, exits `124` from `timeout`'s perspective, and is treated
exactly like any other probe failure by the caller — exit `2`, or `1` with
`--strict` — never as a hang.

### Why a segfault there is still just "a failure," not a crash

That the probe can crash outright, not just throw, is why it runs inside the
same subprocess-isolated `verify_runner_venv` every other check already
used (a fresh `python3 -` per call, not the calling shell): a segfault
there is just a nonzero exit status to bash, handled by the same `if
verify_runner_venv ...; then` this script already had — no special crash
handling needed, because the process boundary already provides it. Confirmed
directly, not assumed: one of the sabotage runs during development (SFPI's
`compiler/` directory renamed away, leaving a stale-but-present receipt)
triggered a real segfault rather than a clean exception, and the script
still reported the correct degraded state and exit code.

### What was actually tested, and how, without touching this shared box's driver

The driver-absent branch could be reproduced live by unloading `tt-kmd` —
and deliberately was not, on a box other Tenstorrent projects are actively
using. Instead, the check's *inputs* were faked, in increasing order of
fidelity:

1. The PCI-counting function itself (`physical_tt_pci_device_count`, in the
   real probe source, parameterized on its sysfs root specifically so it's
   testable this way) was pointed at a hand-built fake `/sys/bus/pci/devices`
   tree with two fabricated `0x1e52` vendor files and one unrelated vendor —
   confirmed it counts exactly the two Tenstorrent ones, and separately that
   an empty or nonexistent root counts zero rather than erroring.
2. The exact probe source extracted verbatim from the script was run through
   the real `venv-runner` interpreter with only `ttnn.get_num_devices` — one
   line — monkeypatched to return `0` *after* a completely genuine `import
   ttnn`. Real `torch`, real `tt_bio`, real `tt_bio.tenstorrent`, and real PCI
   detection (this box's actual 4 cards, read from the real
   `/sys/bus/pci/devices`) all stayed untouched; only the single value the
   fix is designed to distrust was faked. Result: `VERIFY-FAIL: 4
   Tenstorrent PCI device(s) present but ttnn.get_num_devices() reports 0 --
   the tt-kmd driver is most likely not loaded or not bound`, exit `1` from
   the probe (exit `2` from the full script without `--strict`, confirmed
   separately through the normal idempotent-check code path). No `rmmod`, no
   `/dev/tenstorrent` write, no driver state touched at any point.
3. The genuine no-hardware case was verified the same way as before this
   review round: a fully synthetic `ttnn` stub (`get_num_devices() -> 0`,
   `open_device` raising if ever called) combined with an empty fake sysfs
   root, confirming `device probe SKIPPED (0 Tenstorrent PCI devices
   detected)` and exit `0`.
4. The real happy path — genuine hardware, genuine driver, genuine SFPI —
   reports `device probe OK (4 PCI device(s) present, 4 usable,
   opened+closed device 0)`, unaffected by any of the above.
5. The timeout mechanism (`timeout --kill-after=...`) was verified in
   isolation against a Python process that sleeps forever, confirming it
   returns exit `124` and that the wrapping shell code translates that into
   the documented `VERIFY-FAIL: device probe timed out` message — not
   against a real wedged card, since none was available (nor would
   deliberately wedging one on shared hardware be reasonable).

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
  7.35.3, and probes a real device: `device probe OK (4 PCI device(s)
  present, 4 usable, opened+closed device 0)`. Exit 0.
- Re-run immediately after: both venvs detected as already valid (SFPI via
  its receipt, no re-download), no rebuild, exit 0, ~3s.
- `--force`: both venvs discarded and rebuilt from scratch, including a
  fresh SFPI download+verify+install; exit 0, device probe OK again
  afterward.
- The tt_bio-only false-positive repro (from an earlier review round), against
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
- **The PCI-vs-driver mismatch (the Critical fix from the second review
  round)** — see "the device probe" above for the full write-up; summarized
  here for the record: (1) `physical_tt_pci_device_count` against a
  fabricated sysfs tree (two fake `0x1e52` devices, one unrelated vendor,
  and separately an empty/nonexistent root) correctly counts 2, 0, and 0.
  (2) The verbatim probe source, run through the real `venv-runner`
  interpreter with only `ttnn.get_num_devices` monkeypatched to `0` after a
  genuine `import ttnn` — real `torch`, `tt_bio`, `tt_bio.tenstorrent`, and
  real PCI detection (this box's actual 4 cards) all untouched — correctly
  reports `VERIFY-FAIL: 4 Tenstorrent PCI device(s) present but
  ttnn.get_num_devices() reports 0` and fails, where the pre-fix code would
  have reported success. (3) The genuine no-hardware case (fully synthetic
  `ttnn` stub + empty fake sysfs root) still reports `device probe SKIPPED
  (0 Tenstorrent PCI devices detected)` and exit `0`. No kernel module was
  loaded, unloaded, or otherwise touched at any point; the real driver and
  real hardware on this shared box were never disturbed.
- **The device-probe timeout**: verified in isolation (`timeout
  --kill-after=2s 2s python3 -c "time.sleep(999)"`, and the exact
  `rc -eq 124` handling shape used in `verify_runner_venv`) — confirms
  `timeout` reports exit `124` on expiry and that the wrapping code
  translates that into the documented `VERIFY-FAIL: device probe timed
  out...` message. Not exercised against a real wedged card, since none was
  available and deliberately wedging one on shared hardware isn't
  reasonable — the mechanism (a subprocess timeout, handled the same way any
  other nonzero exit already was) doesn't depend on what caused the hang.
- **SFPI's download-before-destroy ordering (the other Critical fix)**:
  against three scratch copies of `venv-runner`, each with an untrusted
  `runtime/sfpi` (receipt removed, a `MARKER` file planted to prove
  survival) — (1) a broken download URL, (2) a corrupted expected hash, and
  (3) a locally-served corrupt tarball (via a `file://` fixture, to make
  `curl` succeed but `tar` fail) were each tried in turn. All three died
  with a named error *and* left the untrusted directory, `MARKER` included,
  completely untouched — no `.old`/`.staging` litter either. A subsequent
  successful run against the same copy then confirmed the swap completes
  cleanly: `MARKER` gone, a correct receipt in place, `device probe OK`,
  exit `0`.
- **Untrusted-directory replacement** (still holds after the reorder above):
  hand-placed a fake `runtime/sfpi` (real subdirectory names, bogus
  contents, no receipt file). The script logged it as untrusted and
  replaced it with a real, verified install rather than assuming its mere
  presence meant it was fine.
- `safe_rm_rf` against a directory made unwritable to simulate a permissions
  failure: dies with a named remedy (exit `1`) instead of a raw, unexplained
  `rm: Permission denied` under `set -e`.
- `scripts/test.sh` (i.e. the 83-test suite through `venv-ui`): all pass.
- `.venvs/venv-ui/bin/python3 -m ui.app` under `WAYLAND_DISPLAY=wayland-0
  DISPLAY=:0`: window opens, no stack trace, only a benign
  `Gtk-WARNING: Unknown key gtk-modules in .../gtk-4.0/settings.ini` (pre-existing,
  unrelated to this change). No process left running afterward.
