# Release CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tagging `v0.1.0` builds the four `.deb` packages and publishes them as a GitHub Release, having first proved they build and install.

**Architecture:** One GitHub Actions workflow with two jobs — `build` (every trigger; gated on the packaging tests plus version consistency) and `release` (tag refs only, the only job with write permission). No venv is built and no application code runs: the gate answers *"does it build, and will apt install it"* and nothing else.

**Tech Stack:** GitHub Actions, `ubuntu-24.04`, `dpkg-buildpackage`/`debhelper`, Docker (`ubuntu:24.04` throwaway containers), `gh` CLI, pytest, system Python 3.12.

**Spec:** `docs/superpowers/specs/2026-08-17-release-ci-design.md` (see §4a — the gate was narrowed after the first draft).

## Global Constraints

- **Runner is `ubuntu-24.04`, pinned.** Never `ubuntu-latest` — `debian/changelog` targets `noble` and the build environment is part of the contract.
- **Never touch hardware.** No `/dev/tenstorrent`, no `--device` passed to any container, no `tests/integration`.
- **Never run `tt-bio install-deps`.** Nothing installs kernel modules.
- **Build no venv in CI**, and install no GTK/OpenGL/torch dependency. The gate's modules import only `re`, `subprocess`, `pathlib`, `shutil`, `tempfile`, `pytest`.
- **`scripts/test.sh` is not modified by this plan.** An earlier draft added `--ui-only`; the narrowed gate removed the need for it.
- **A skipped check must never look like a passed one.** Missing Docker or missing `pdftotext` fails; it never skips.
- **No apt repository, no GPG signing.** Out of scope.
- Repo root in tests is `pathlib.Path(__file__).resolve().parents[2]`, following `tests/unit/test_packaging.py`.

---

## File Structure

| File | Responsibility |
|---|---|
| `tests/unit/test_packaging.py` (modify) | Gains one test: the metapackage installs and all four packages land |
| `tests/unit/test_version_consistency.py` (create) | `VERSION` ↔ `debian/changelog` ↔ the handout PDF |
| `.github/workflows/packages.yml` (create) | `build` on every trigger, `release` on tags |

Task 1 is the one that answers the question this CI exists for; Tasks 2 and 3 make it run automatically and turn a tag into a release.

---

### Task 1: Prove the metapackage actually installs

The existing container tests install `tt-bio-demo-runtime` individually under various debconf answers. Nothing installs `tt-bio-demo-all` and checks that all four packages end up installed — which is exactly the "will a debian file install" question.

**Files:**
- Modify: `tests/unit/test_packaging.py` (append; it already has the `built` and `container` fixtures)

**Interfaces:**
- Consumes: the existing `container` fixture from `tests/unit/conftest.py` (re-exported from `conftest_container.py`), whose `.install(pkg, preseed=…)` returns an object with `.installed`, `.log` and `.status(pkg) -> str | None`.
- Produces: no importable API. Task 3 runs this file as the gate.

- [ ] **Step 1: Read the harness contract before writing against it**

Run: `sed -n '190,270p' tests/unit/conftest_container.py`

Confirm the exact signature of `Container.install` and what `ContainerResult.status()` returns for a package that installed cleanly (`"install ok installed"`). Do not guess these — the test below asserts on them.

- [ ] **Step 2: Write the failing test**

Append to `tests/unit/test_packaging.py`:

```python
def test_the_metapackage_installs_and_brings_all_four_with_it(container):
    """The question this repo's CI exists to answer: will a .deb install?

    Every other container test here installs ONE package to check one
    behaviour -- what a debconf answer does, whether install-deps ran. None of
    them asks the plain question an operator asks, which is whether
    `apt install tt-bio-demo-all` on a clean machine ends with four installed
    packages and no broken state.

    Preseeded to decline both prompts, because those are the DEFAULTS and
    therefore the path an unattended install actually takes. Declining must
    leave a complete, installed set -- the downloads and the venv build are
    later, deliberate steps (see INSTALL.md), not preconditions for the
    packages being installed.
    """
    result = container.install("tt-bio-demo-all", preseed={
        "tt-bio-demo-runtime/install-deps": "boolean false",
        "tt-bio-demo-weights/download": "boolean false",
    })
    assert result.installed, result.log
    for pkg in sorted(EXPECTED):
        assert result.status(pkg) == "install ok installed", (
            f"{pkg} is {result.status(pkg)!r} after installing the metapackage; "
            f"apt did not end up with all four installed.\n{result.log}"
        )
```

- [ ] **Step 3: Run it**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_packaging.py -k metapackage_installs -v`

Expected: PASS if the packaging is already sound, FAIL if it is not. **Either outcome is informative and neither is a reason to change the test.** If it fails, read `result.log` — you have found the real defect this whole plan was built to catch, and fixing the packaging is the correct next move. Report it rather than weakening the assertion.

- [ ] **Step 4: Prove the test can fail**

A test that has never failed is not yet a test. Temporarily assert against a package that is not installed:

```bash
python3 - <<'PY'
import pathlib
p = pathlib.Path("tests/unit/test_packaging.py"); s = p.read_text()
p.write_text(s.replace('for pkg in sorted(EXPECTED):',
                       'for pkg in sorted(EXPECTED | {"coreutils-not-installed"}):'))
PY
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_packaging.py -k metapackage_installs -q
# Expected: FAIL, naming coreutils-not-installed as None
git checkout tests/unit/test_packaging.py   # discard BOTH the probe and the test
```

Then re-apply Step 2's test (the `git checkout` removes it too) and re-run Step 3 to confirm it passes again.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_packaging.py
git commit -m "test: the metapackage installs, and brings all four with it

Every other container test installs one package to check one behaviour. None
asked the plain question an operator asks -- whether apt install
tt-bio-demo-all on a clean machine ends with four installed packages.

Preseeded to decline both prompts, because those are the defaults and so the
path an unattended install takes. Declining has to leave a complete installed
set: the weights download and the venv build are later deliberate steps, not
preconditions for the packages being installed."
```

---

### Task 2: The version invariant, as tests

**Files:**
- Create: `tests/unit/test_version_consistency.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: three tests. Task 3 runs this file alongside `test_packaging.py`.

- [ ] **Step 1: Write the tests**

Create `tests/unit/test_version_consistency.py`:

```python
"""One version, four places to write it down.

    VERSION                          the repo's own answer
    debian/changelog (top stanza)    what a .deb will call itself
    docs/tt-bio-demo-onepager.pdf    what the printed handout claims
    the git tag                      what a release is named

The first three are checked here, so they fail on a developer's machine and
not only in CI. The fourth is checked in .github/workflows/packages.yml,
because only CI has a tag.

The PDF is the non-obvious one and the reason this module exists.
docs/onepager/build.sh stamps VERSION into the sheet at render time, so
bumping VERSION without re-running that script ships a handout claiming the
previous version -- on the one artefact most likely to be printed and handed
to a stranger, where nobody would think to check.

Needs no venv, no device and no package build -- which is why it is here
rather than in test_packaging.py, where every test waits on a module-scoped
dpkg-buildpackage that three string comparisons do not need.
"""
import pathlib
import re
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
VERSION_FILE = REPO / "VERSION"
CHANGELOG = REPO / "debian" / "changelog"
HANDOUT = REPO / "docs" / "tt-bio-demo-onepager.pdf"

# `tt-bio-demo (0.1.0) noble; urgency=medium`
_CHANGELOG_TOP = re.compile(r"^tt-bio-demo \(([^)]+)\)")


def _version_file():
    return VERSION_FILE.read_text().strip()


def _changelog_version():
    first = CHANGELOG.read_text().splitlines()[0]
    m = _CHANGELOG_TOP.match(first)
    assert m, f"could not parse the top changelog stanza: {first!r}"
    return m.group(1)


def test_the_changelog_agrees_with_the_version_file():
    assert _changelog_version() == _version_file(), (
        f"debian/changelog says {_changelog_version()!r} but VERSION says "
        f"{_version_file()!r}. Both are hand-written; pick the right one and "
        "make the other match."
    )


def test_the_handout_pdf_was_rebuilt_for_this_version():
    version = _version_file()
    assert HANDOUT.is_file(), f"{HANDOUT} is missing"
    if shutil.which("pdftotext") is None:
        pytest.fail(
            "pdftotext is not installed, so the handout's version could not be "
            "checked. Install poppler-utils. This is a failure rather than a "
            "skip on purpose: a silently skipped check here is indistinguishable "
            "from a passing one."
        )
    text = subprocess.run(
        ["pdftotext", str(HANDOUT), "-"],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout
    assert f"v{version}" in text, (
        f"the handout does not mention v{version}. VERSION was probably bumped "
        "without re-rendering it -- run docs/onepager/build.sh and commit the "
        "result."
    )


def test_the_version_is_a_plain_three_part_number():
    """Guards the tag comparison in CI, which strips exactly one leading 'v'."""
    version = _version_file()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
        f"VERSION is {version!r}; the release workflow compares it against a "
        "tag with one leading 'v' stripped, so anything else needs that "
        "comparison revisited first."
    )
```

- [ ] **Step 2: Run them**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_version_consistency.py -v`
Expected: 3 passed — the repo is currently consistent at `0.1.0`.

- [ ] **Step 3: Prove each one bites**

```bash
sed -i '1s/(0.1.0)/(0.9.9)/' debian/changelog
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_version_consistency.py -x -q
# Expected: test_the_changelog_agrees_with_the_version_file FAILS
git checkout debian/changelog

echo "0.2.0" > VERSION
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_version_consistency.py -q
# Expected: the changelog AND handout tests both FAIL
git checkout VERSION
```

- [ ] **Step 4: Confirm the tree is clean**

Run: `git status --porcelain`
Expected: only `tests/unit/test_version_consistency.py`, untracked. If `VERSION` or `debian/changelog` appear, the `git checkout`s did not run — restore them before committing.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_version_consistency.py
git commit -m "test: one version, and the four places it has to agree

VERSION, debian/changelog and the handout PDF are checked here so they fail on
a developer's machine; the git tag is checked in CI, the only place a tag
exists.

The PDF is why this exists. docs/onepager/build.sh stamps VERSION into the
sheet, so bumping VERSION without re-rendering ships a handout claiming the old
version -- on the artefact most likely to be printed and handed to a stranger.
pdftotext catches it, and its absence FAILS rather than skips: a silently
skipped check reads exactly like a passing one."
```

---

### Task 3: The workflow

**Files:**
- Create: `.github/workflows/packages.yml`

**Interfaces:**
- Consumes: `tests/unit/test_packaging.py` (Task 1), `tests/unit/test_version_consistency.py` (Task 2), and the existing `scripts/build-deb.sh`.
- Produces: a GitHub Release on tags carrying `dist/*.deb`, `dist/*.buildinfo`, `dist/*.changes` and `docs/tt-bio-demo-onepager.pdf`.

- [ ] **Step 1: Create the workflow**

Create `.github/workflows/packages.yml`:

```yaml
# Builds the four Debian packages on every push, and publishes them as a
# GitHub Release when the ref is a tag.
#
# Design: docs/superpowers/specs/2026-08-17-release-ci-design.md
#
# WHAT THIS GATE IS FOR, precisely: "if we make a debian file, is it going to
# be installable?" It runs the packaging tests -- which build the real .debs
# and install them into throwaway ubuntu:24.04 containers -- and the version
# consistency tests. That is all.
#
# What it deliberately does NOT do:
#   - build venv-ui or venv-runner, or install any GTK, OpenGL or torch
#     dependency. The .debs compile nothing and carry no Python runtime; both
#     venvs are built at install time by setup-venvs.sh. Application
#     correctness lives in ./scripts/test.sh, run locally before tagging.
#   - touch Tenstorrent hardware, or pass --device to any container.
#   - run `tt-bio install-deps`, which installs kernel modules.
#   - publish an apt repository or sign anything.
name: packages

on:
  push:
    branches: [main]
    tags: ['v*']
  pull_request:
    branches: [main]
  workflow_dispatch:

# Read-only by default. Only the release job widens this, and only to create
# the release.
permissions:
  contents: read

jobs:
  build:
    # PINNED, never ubuntu-latest: debian/changelog targets noble and
    # debian/control names noble-era packages, so the build environment is
    # part of the contract. Moving it must be a deliberate edit here.
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4

      # First, and before anything slow: a mis-tagged release should fail in
      # seconds. VERSION's agreement with the changelog and the handout is
      # checked by the test suite below -- only the tag needs CI, because only
      # CI has one.
      - name: The tag must agree with VERSION
        if: startsWith(github.ref, 'refs/tags/')
        run: |
          tag_version="${GITHUB_REF_NAME#v}"
          file_version="$(cat VERSION)"
          if [ "$tag_version" != "$file_version" ]; then
            echo "::error::tag ${GITHUB_REF_NAME} means version ${tag_version}, but VERSION says ${file_version}"
            exit 1
          fi
          echo "tag ${GITHUB_REF_NAME} agrees with VERSION ${file_version}"

      # Deliberately short. debhelper/devscripts/dpkg-dev build the packages;
      # poppler-utils backs the handout version check; python3-pytest runs the
      # tests under the SYSTEM interpreter -- the gate's two modules import
      # only re, subprocess, pathlib, shutil, tempfile and pytest, so no venv
      # is needed. Docker is preinstalled on this runner and the container
      # tests fail rather than skip without it.
      - name: Install build and test dependencies
        run: |
          sudo apt-get update
          sudo apt-get install -y --no-install-recommends \
            debhelper devscripts dpkg-dev \
            poppler-utils \
            python3-pytest

      # The gate: do the packages build, and do they install. tests/unit/
      # conftest.py re-exports the container fixture, so running the two files
      # from the repo root is enough.
      - name: Gate — the packages build and install
        run: |
          python3 -m pytest -v \
            tests/unit/test_packaging.py \
            tests/unit/test_version_consistency.py

      - name: Build the packages
        run: ./scripts/build-deb.sh

      - name: Upload the packages
        uses: actions/upload-artifact@v4
        with:
          name: packages
          path: dist/
          if-no-files-found: error

  release:
    needs: build
    if: startsWith(github.ref, 'refs/tags/')
    runs-on: ubuntu-24.04
    # The only place in this file that can write to the repository.
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4

      # Downloaded, never rebuilt: the artefact that was tested is the
      # artefact that gets published.
      - name: Download the packages built and tested above
        uses: actions/download-artifact@v4
        with:
          name: packages
          path: dist

      # The changelog stanza IS the release notes. It is hand-written Debian
      # prose describing exactly this version; generating notes from commits
      # instead would publish this project's long internal commit messages.
      - name: Release notes from debian/changelog
        run: |
          dpkg-parsechangelog -l debian/changelog -S Changes > release-notes.md
          echo "--- notes ---"
          cat release-notes.md

      # A published .deb is never replaced. Someone may already have installed
      # it, and package managers are built on the assumption that a fixed
      # version number means fixed contents.
      - name: Refuse to replace an existing release
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          if gh release view "${GITHUB_REF_NAME}" >/dev/null 2>&1; then
            echo "::error::release ${GITHUB_REF_NAME} already exists; refusing to replace it. Bump the version and tag again."
            exit 1
          fi

      - name: Create the release
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh release create "${GITHUB_REF_NAME}" \
            --title "tt-bio-demo ${GITHUB_REF_NAME}" \
            --notes-file release-notes.md \
            dist/*.deb \
            dist/*.buildinfo \
            dist/*.changes \
            docs/tt-bio-demo-onepager.pdf
```

- [ ] **Step 2: Check the YAML parses and says what it should**

```bash
python3 -c "
import yaml, pathlib
d = yaml.safe_load(pathlib.Path('.github/workflows/packages.yml').read_text())
assert list(d['jobs']) == ['build', 'release'], list(d['jobs'])
assert d['jobs']['build']['runs-on'] == 'ubuntu-24.04'
assert d['jobs']['release']['runs-on'] == 'ubuntu-24.04'
assert d['permissions']['contents'] == 'read'
assert d['jobs']['release']['permissions']['contents'] == 'write'
steps = ' '.join(str(s) for s in d['jobs']['build']['steps'])
assert 'setup-venvs' not in steps, 'the gate must build no venv'
assert 'gir1.2-gtk' not in steps, 'the gate must install no GTK stack'
print('ok')
"
```

Expected: `ok`.

Note: `on:` parses as the boolean key `True` under YAML 1.1. That is a loader quirk, not a defect — do not "fix" it. If PyYAML is unavailable, `pip install --user pyyaml` or skip this step and rely on the first CI run.

- [ ] **Step 3: Run the exact gate command locally**

Run:

```bash
python3 -m pytest -v tests/unit/test_packaging.py tests/unit/test_version_consistency.py
```

Expected: all pass, using the **system** python3 with no venv — proving the CI invocation works. If `python3 -m pytest` reports no pytest, `sudo apt-get install python3-pytest` (this mirrors exactly what the workflow does).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/packages.yml
git commit -m "ci: build the packages on every push, publish them on a tag

One workflow. build runs everywhere and is gated on exactly the question this
CI exists to answer -- do the packages build, and will apt install them --
which is the packaging tests plus version consistency, and nothing else. No
venv is built and no GTK or torch dependency is installed; the .debs compile
nothing and carry no Python runtime, so application correctness stays in
./scripts/test.sh, run locally before tagging.

release runs on tag refs only and is the one job with contents: write. The
tag/VERSION check runs first so a mis-tagged release dies in seconds. Notes are
the debian/changelog stanza via dpkg-parsechangelog, where generated notes
would publish our internal commit messages. It downloads rather than rebuilds,
so what was tested is what ships, and refuses to replace an existing release."
```

- [ ] **Step 5: Push and watch the first run**

```bash
git push origin main
gh run watch
```

Expected: `build` succeeds; `release` is skipped (not a tag). If `build` fails, read the failing test before changing the workflow — a genuine packaging defect is the outcome this plan was written to surface.

---

## What this plan does NOT do

Cut the first release, or update the docs afterwards. `INSTALL.md` step 1 and README's "Installing a booth machine" still tell a reader to build the packages themselves — correct until a release exists to link to. `VERSION` stays at `0.1.0`; tagging is a human decision.

---

## Self-Review

**Spec coverage.** §2 trigger/shape → Task 3 Step 1. §3 version invariant → Task 2 (three checks) and Task 3 Step 1 (the tag). §4 gate, and the metapackage-install gap it identifies → Task 1, run by Task 3. §4a's deletions → honoured: no `--ui-only`, no venv, no GTK, asserted mechanically in Task 3 Step 2. §5 jobs and permissions → Task 3. §6 failure modes: version mismatch (Task 2), tag mismatch (Task 3), stale PDF (Task 2), Docker missing (inherited from the harness), release exists (Task 3), metapackage install failure (Task 1). §7 doc consequences → excluded above, deliberately.

**Placeholder scan.** No TBDs. Every code step carries real code; every run step carries its command and expected output.

**Name consistency.** `EXPECTED` in Task 1 is the existing module-level set in `test_packaging.py` (`{"tt-bio-demo", "tt-bio-demo-runtime", "tt-bio-demo-weights", "tt-bio-demo-all"}`) — not redefined. `result.status(pkg)`/`result.installed`/`result.log` match `ContainerResult` in `conftest_container.py`. Artifact name `packages` matches between upload and download. The two test paths in Task 3's gate match the files created in Tasks 1 and 2.

**One thing the implementer must not do.** If Task 1's new test fails, that is a real packaging defect and the finding this plan exists to produce. Report it; do not relax the assertion to make the gate green.
