# Release CI — build the Debian packages for every release

**Status:** design, approved 2026-08-17. **Revised the same day** — see §4a.
**Goal:** tagging `v0.1.0` produces a GitHub Release carrying the four `.deb` packages,
so `INSTALL.md` step 1 stops saying "build them yourself first".

> **§4a — Scope revision (2026-08-17, after the first draft).**
> The gate was narrowed on instruction: *"focus the CI jobs on overall 'it should work'
> quality. We don't need the UI validated. We don't need the hardware validated. We just
> need to know that if we make a debian file it's going to be installable."*
>
> The original §4 gated on the `venv-ui` half of the suite — 1115 tests, a GTK/OpenGL apt
> stack, and a whole venv build — to protect a `.deb` that contains no Python runtime and
> no UI code that CI could exercise. That is off-target for the stated question.
>
> **The gate is now: do the packages build, and do they install.** Which deletes two things
> outright:
> - **`test.sh --ui-only` is no longer needed and is not built.** It existed only to run the
>   UI half without `venv-runner`. With no UI half in the gate there is no flag to add, and
>   `scripts/test.sh` is left untouched.
> - **No venv is built in CI at all**, and none of the GTK/OpenGL apt packages are installed.
>   At module scope, `tests/unit/test_packaging.py` and `tests/unit/conftest_container.py`
>   import only `re`, `subprocess`, `pathlib`, `shutil`, `tempfile` and `pytest` — but
>   `test_packaging.py` also has a function-local `import yaml` inside two tests
>   (`test_every_playlist_input_ships`, `test_every_playlist_thumbnail_ships`), which the
>   original scan of top-level imports missed entirely. The system interpreter plus
>   `python3-pytest` **and `python3-yaml`** is what actually runs them.
>
> §4 and §5 below are rewritten accordingly. §8's headless measurement is kept as a recorded
> fact but no longer gates anything.

---

## 1. What this is for

`INSTALL.md` currently opens by telling a booth operator to build the packages, because
`dist/` is gitignored and no repository publishes them. That is the wrong order of
operations for a machine that is about to leave for a conference: the person imaging it
should be downloading a tested artefact, not running a build toolchain.

This design makes a git tag the thing that produces those artefacts, and makes every push
prove they still build.

### Explicitly not in scope

- **No apt repository and no GPG signing.** `sudo apt install tt-bio-demo-all` by name stays
  unavailable, and README's "Not yet built" list keeps its apt-repository entry. Publishing a
  repo needs a signing key, a keyring-distribution story for booth machines, and repo
  metadata generation — a separate piece of work, not a rider on this one.
- **No hardware, ever.** The workflow never opens a Tenstorrent device and never passes
  `--device` to a container. A package that only works with silicon attached cannot be
  verified at install time anyway; the booth's own preflight covers that.
- **Never runs `tt-bio install-deps`.** Same rule the build script and the postinst already
  follow: nothing installs kernel modules without a human asking for it.

---

## 2. Trigger and shape

One workflow file, `.github/workflows/packages.yml`:

```yaml
on:
  push:
    branches: [main]
    tags: ['v*']
  pull_request:
    branches: [main]
  workflow_dispatch:
```

It always builds and tests. It publishes **only** when the ref is a tag. One file rather than
a `ci.yml`/`release.yml` pair because the gate would otherwise exist in two places that drift
apart; `workflow_dispatch` exists so the workflow itself can be exercised without inventing a
tag, and it never publishes.

**Runner: `ubuntu-24.04`, pinned.** Not `ubuntu-latest`. `debian/changelog` targets `noble`
and `debian/control` names noble-era packages, so the build environment is part of the
contract. A runner-image bump must be a deliberate edit here, not something that arrives on
its own and silently retargets the distro.

---

## 3. The version invariant

A version lives in **four** places in this repo, and all four can drift independently:

| Source | Where | Checked |
|---|---|---|
| `VERSION` | repo root | always |
| `debian/changelog` top stanza | packaging | always |
| the git tag, minus its `v` | the release itself | on tags only |
| **the committed one-pager PDF** | `docs/tt-bio-demo-onepager.pdf` | always |

The fourth is the one worth calling out, because it is not obvious and it is already
reachable today. `docs/onepager/build.sh` reads `VERSION` and stamps it into the sheet, so
bumping `VERSION` without re-running that script ships a handout claiming the previous
version — on the artefact most likely to be printed and handed to a stranger. It is
detectable cheaply:

```bash
pdftotext docs/tt-bio-demo-onepager.pdf - | grep -q "v$(cat VERSION)"
```

which costs one `poppler-utils` install.

**Where each check lives matters.** The three checks that need no tag —
`VERSION` ↔ changelog ↔ PDF — live in `tests/unit/test_version_consistency.py`, so they run
on a developer's machine during an ordinary `./scripts/test.sh` and not only in CI. That is
this project's standing rule: a check that can only fail in CI is a check most people never
see fail. Only the tag comparison is CI-only, because only CI has a tag.

Their own module rather than `test_packaging.py`: every test there depends on a
module-scoped `built` fixture that runs a full `dpkg-buildpackage`, and three string
comparisons have no business waiting on a package build.

**On mismatch the run fails.** It does not auto-correct, and it does not warn. Version bumps
in this repo are deliberate, hand-written acts — the changelog stanza is Debian prose someone
wrote — and a build that quietly rewrites them would destroy the thing that makes them worth
reading.

---

## 4. The gate

**Gate = the packages build, and the packages install.** Nothing else.

The question this CI exists to answer is *"if we make a debian file, is it going to be
installable?"* — so the gate is `tests/unit/test_packaging.py` (57 tests, including roughly
ten that install into a throwaway `ubuntu:24.04` via `scripts/deb-container.sh`) plus the
version-consistency tests of §3.

**No venv is built.** At module scope those files import only `re`, `subprocess`,
`pathlib`, `shutil`, `tempfile` and `pytest` — but `test_packaging.py` also carries a
function-local `import yaml` inside two tests, easy to miss when scanning only a module's
top-level imports (which is exactly how the original version of this design missed it). So
the system interpreter plus apt's `python3-pytest` **and `python3-yaml`** runs them.
`tests/unit/conftest.py` re-exports the `container` fixture, so invoking pytest on the two
files from the repo root is enough.

**If Docker is unavailable those tests must fail, not skip**, per the rule already written
into the packaging plan: a silently skipped install test is indistinguishable from a passing
one. The harness already behaves this way; CI must not weaken it.

### The one test the gate cannot run

`test_the_weights_postinst_uses_the_tt_bio_api_that_actually_exists` parses `tt_bio/main.py`
out of **`venv-runner`'s** site-packages, to check the weights postinst calls an API that
still exists. It needs the multi-GB venv this workflow deliberately never builds, and it
correctly `pytest.fail`s rather than skips when the venv is missing — which is what turned
the first green-ish run red.

It is therefore **`--deselect`ed by name on the CI command line**, not skipped. The
distinction matters: the deselection is one visible line in the workflow and in the run log,
and the test still fails properly for anyone running the full suite locally, where
`venv-runner` exists. A second test needing exclusion would be a signal to re-examine this
scope rather than to lengthen the list.

### The one real gap in existing coverage

The container tests install packages *individually* — `tt-bio-demo-runtime` under various
debconf answers. **Nothing installs `tt-bio-demo-all`**, the metapackage, and asserts that
all four packages end up installed. That is precisely the question being asked, so this
design adds it: one test that installs the metapackage into a fresh container and requires
every one of the four to reach `install ok installed`.

### What the gate does not cover, and why

Neither half of the application suite runs. No `venv-ui`, no `venv-runner`, no GTK, no
torch/ttnn, no hardware, no `tests/integration`.

This is **deliberate and instructed**, not an oversight. The `.deb`s compile nothing, carry
no Python runtime, and build both venvs at install time from `setup-venvs.sh` — so what CI
can meaningfully prove about them is that they build, that their metadata and contents are
right, and that `apt` will install them. Application correctness stays where it already
lives: `./scripts/test.sh` with no flags, run locally before tagging.

---

## 5. Jobs

### `build` — runs on every trigger

`permissions: contents: read`.

1. Check out.
2. **Tag refs only, and first:** assert the tag matches `VERSION`, so a mis-tagged release
   fails in seconds rather than after the gate.
3. `apt-get install` the build and test dependencies only: `debhelper`, `devscripts`,
   `dpkg-dev`, `poppler-utils`, `python3-pytest`. No GTK, no OpenGL, no venv tooling.
4. The gate: `python3 -m pytest tests/unit/test_packaging.py tests/unit/test_version_consistency.py`.
5. `scripts/build-deb.sh`.
6. Upload everything in `dist/` as a workflow artifact.

### `release` — tags only

`needs: build`, `if: startsWith(github.ref, 'refs/tags/')`, and the **only** job with
`permissions: contents: write`.

1. Download the artifact from `build`. It does **not** rebuild — the thing tested is the
   thing published.
2. Release notes: the matching changelog stanza, via `dpkg-parsechangelog -S Changes`. Not
   auto-generated from commits; this project's commit messages are long and internal and
   would read poorly as public notes.
3. Attach: the four `.deb`s, the `.buildinfo` and `.changes` that `build-deb.sh` already
   sweeps into `dist/` (free build provenance), and `docs/tt-bio-demo-onepager.pdf`.

**Re-tagging an existing version fails.** A published release is never silently replaced —
someone may already have downloaded it, and a `.deb` whose contents changed under a fixed
version number is the specific thing package managers are built to trust.

---

## 6. Failure modes

| Situation | Behaviour |
|---|---|
| Any two version sources disagree | Fail, in the test suite, locally and in CI |
| Tag disagrees with `VERSION` | Fail first, before the gate spends any time |
| One-pager PDF stamped with an older version | Fail — re-run `docs/onepager/build.sh` and commit |
| Docker unavailable to the container harness | Fail loudly; never skip |
| Release already exists for this tag | Fail; do not replace |
| Any application-code regression, UI or runner | **Not caught, by instruction.** CI answers "does it build and install"; local `./scripts/test.sh` covers correctness |
| `tt-bio-demo-all` fails to install | Fail — the new metapackage install test is the point of the gate |

---

## 7. Consequences for the docs

Once this lands, `INSTALL.md` step 1 changes from "build them on a dev box and copy them
over" to "download the four `.deb`s from the release page", with the build path kept as the
fallback for an unreleased commit. README's "Installing a booth machine" follows. Neither
change belongs in this spec's implementation — they are a follow-up once a real release
exists to link to.

---

## 8. The display question, settled — and now moot

> **Superseded by §4a.** The UI half no longer runs in CI at all, so nothing below gates
> anything. It is kept because the measurement is real and worth having on record the next
> time someone asks whether this suite needs a display.


The one risk in this design was whether the `venv-ui` half needs a display on a headless
runner — a GTK4 suite is a fair thing to suspect, and getting it wrong means the whole gate
fails on the first CI run for a reason unrelated to the code.

**Measured rather than assumed.** The half was run with both `DISPLAY` and `WAYLAND_DISPLAY`
unset:

```bash
env -u DISPLAY -u WAYLAND_DISPLAY .venvs/venv-ui/bin/python3 \
    -m pytest tests/unit --ignore=tests/unit/runner -q
# 1115 passed in 228.15s (0:03:48)
```

**No `xvfb-run` is needed**, and the workflow must not add one speculatively. The suite is
built around pure-logic modules — slot state, geometry, secondary structure, the protocol —
that never open a window, which is why it survives having no display at all.

Worth keeping in mind that this was measured on a desktop machine that merely had its display
variables hidden, not on a machine with no display stack installed. A GitHub runner has the
GTK libraries but no session; if the first CI run contradicts this, `xvfb-run` around the
gate is the one-line fix and nothing else in this design changes.
