# Release CI — build the Debian packages for every release

**Status:** design, approved 2026-08-17.
**Goal:** tagging `v0.1.0` produces a GitHub Release carrying the four `.deb` packages,
so `INSTALL.md` step 1 stops saying "build them yourself first".

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
`VERSION` ↔ changelog ↔ PDF — belong in `tests/unit/test_packaging.py`, so they run on a
developer's machine during an ordinary `./scripts/test.sh` and not only in CI. That is this
project's standing rule: a check that can only fail in CI is a check most people never see
fail. Only the tag comparison is CI-only, because only CI has a tag.

**On mismatch the run fails.** It does not auto-correct, and it does not warn. Version bumps
in this repo are deliberate, hand-written acts — the changelog stanza is Debian prose someone
wrote — and a build that quietly rewrites them would destroy the thing that makes them worth
reading.

---

## 4. The gate

**Gate = the `venv-ui` half of the suite + the packaging tests, including the throwaway
container install harness.** No multi-gigabyte downloads.

**Measured, not estimated:** on this box, `tests/unit` minus `tests/unit/runner` is
**1115 tests in 228 s (3 m 48 s)**, and that figure already includes the packaging and
container-install tests, which live in the same half. Budget 5–10 minutes on a GitHub runner,
which is slower and additionally has to build `venv-ui` and pull the `ubuntu:24.04` image.

This is matched to what actually ships: the `.deb`s compile nothing and carry no Python
runtime, so what needs proving is package metadata, contents, and postinst behaviour — which
is exactly what `scripts/deb-container.sh` and `tests/unit/test_packaging.py` already do
against a disposable `ubuntu:24.04`. **If Docker is unavailable those tests must fail, not
skip**, per the rule already written into the packaging plan: a silently skipped install test
is indistinguishable from a passing one.

### A change this forces: `test.sh --ui-only`

`scripts/test.sh` runs both halves and deliberately treats *a half matching zero tests* as a
failure. CI therefore cannot get a UI-only run by passing a `-k` selector — that is precisely
the case the script is written to reject.

So this design requires a real, small change to `test.sh`: add **`--ui-only`**, which skips
the `venv-runner` half **outright** rather than selecting zero tests from it, and which
**names itself in the verdict line** so a CI log can never be misread as a full-suite pass.

To be unambiguous about what `--ui-only` relaxes: it removes the runner half from the run
entirely, and with it that half's zero-test check. **The UI half keeps every existing rule**,
including that a UI half matching zero tests is still a failure. The flag narrows what is
run; it must not weaken how what is run gets judged.

The alternative — CI invoking pytest directly for `tests/unit --ignore=tests/unit/runner` —
was rejected. The directory-based venv split is a real decision documented in one place, and
copying it into a YAML file is how it ends up maintained in two.

### What the gate does not cover, and why

The `venv-runner` half does not run. Building that venv means torch, ttnn, tt-bio and the
SFPI toolchain — several GB and 10–20 minutes per run, fetched from PyPI and Hugging Face,
with network flakes presenting as failures that are not defects.

This is a **known limitation, not an oversight**, and the workflow says so in a comment. The
daemon source does ship inside `tt-bio-demo`, so it is genuinely less covered than the UI
half. The mitigation is that `./scripts/test.sh` with no flags — the full suite, both halves
— remains what a developer runs locally before tagging.

---

## 5. Jobs

### `build` — runs on every trigger

`permissions: contents: read`.

1. Check out; install build dependencies (`debhelper`, `devscripts`, `poppler-utils`) and
   the apt packages `venv-ui` needs.
2. Assert the tag matches `VERSION` — **tag refs only**, and first, so a mis-tagged release
   fails in seconds rather than after the gate.
3. `scripts/setup-venvs.sh --skip-runner` — the repo-default `.venvs/` prefix, not
   `/opt/tt-bio-demo`; CI is not installing a booth, it is building one venv to test with.
4. The gate: `scripts/test.sh --ui-only` (which carries the three tag-free version checks
   with it, as ordinary tests).
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
| A `venv-runner`-half regression | **Not caught.** Documented limitation; local `test.sh` covers it |

---

## 7. Consequences for the docs

Once this lands, `INSTALL.md` step 1 changes from "build them on a dev box and copy them
over" to "download the four `.deb`s from the release page", with the build path kept as the
fallback for an unreleased commit. README's "Installing a booth machine" follows. Neither
change belongs in this spec's implementation — they are a follow-up once a real release
exists to link to.

---

## 8. The display question, settled

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
