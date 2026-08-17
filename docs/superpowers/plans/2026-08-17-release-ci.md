# Release CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tagging `v0.1.0` builds the four `.deb` packages and publishes them as a GitHub Release, and every push proves they still build.

**Architecture:** One GitHub Actions workflow with two jobs — `build` (runs on every trigger, gated on the `venv-ui` half of the suite plus the packaging tests) and `release` (tag refs only, the only job with write permission). Version consistency across four sources is enforced by ordinary pytest tests so it fails on a developer's machine too, not only in CI.

**Tech Stack:** GitHub Actions, `ubuntu-24.04`, `dpkg-buildpackage`/`debhelper`, `gh` CLI, pytest, bash.

**Spec:** `docs/superpowers/specs/2026-08-17-release-ci-design.md`

## Global Constraints

- **Runner is `ubuntu-24.04`, pinned.** Never `ubuntu-latest` — `debian/changelog` targets `noble` and the build environment is part of the contract.
- **Never touch hardware.** No `/dev/tenstorrent`, no `--device` passed to any container, no `tests/integration`.
- **Never run `tt-bio install-deps`.** Nothing installs kernel modules.
- **Never build `venv-runner` in CI.** It means torch + ttnn + SFPI, several GB per run.
- **A skipped check must never look like a passed one.** Anything that narrows what ran has to name itself in the verdict.
- **No apt repository, no GPG signing.** Out of scope for this plan.
- Repo root in tests is `pathlib.Path(__file__).resolve().parents[2]`, following `tests/unit/test_packaging.py`.

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/test.sh` (modify) | Gains `--ui-only`: runs the UI half alone, requires no `venv-runner`, names itself in the verdict |
| `tests/unit/test_version_consistency.py` (create) | The three tag-free version checks: `VERSION` ↔ `debian/changelog` ↔ the handout PDF |
| `tests/unit/test_test_sh_ui_only.py` (create) | Proves `--ui-only` behaves — no `venv-runner` required, verdict names it, `--hw` rejected |
| `.github/workflows/packages.yml` (create) | The workflow: `build` on everything, `release` on tags |

**One refinement of the spec.** The spec placed the version checks in `tests/unit/test_packaging.py`. They go in their own module instead: every test in `test_packaging.py` depends on the module-scoped `built` fixture, which runs a full `dpkg-buildpackage`. The version checks need no build at all, and binding them to that fixture would make three cheap string comparisons wait on a package build. Same half, same conventions, better boundary.

---

### Task 1: `test.sh --ui-only`

**Files:**
- Modify: `scripts/test.sh` (usage block ~line 84; flag parsing ~line 141; venv requirements ~line 178; half invocation ~line 229; verdict ~line 283)
- Test: `tests/unit/test_test_sh_ui_only.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `scripts/test.sh --ui-only` — exit 0 iff the UI half passes; requires only `${TT_BIO_DEMO_PREFIX}/venv-ui`; prints `runner half: SKIPPED (--ui-only)` and an `OVERALL:` line containing `--ui-only`. Task 3 calls it.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_test_sh_ui_only.py`:

```python
"""`scripts/test.sh --ui-only`: the flag CI needs, and the honesty it owes.

CI builds venv-ui alone -- venv-runner means torch, ttnn and the SFPI
toolchain, several GB per run -- so it needs a way to run just the UI half.
It cannot get one by passing a `-k` selector, because test.sh deliberately
treats a half matching zero tests as a failure (pytest exit 5). Hence a real
flag that removes the runner half outright.

The tests below are mostly about the SECOND half of that: a run which proved
less than a full run must never be readable as a full one.

Runs under venv-ui. Invokes test.sh as a subprocess with --collect-only, so
these cost a collection rather than 1115 real tests.
"""
import os
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
TEST_SH = REPO / "scripts" / "test.sh"


@pytest.fixture(scope="module")
def ui_only_prefix(tmp_path_factory):
    """A venv prefix holding venv-ui and DELIBERATELY NO venv-runner.

    This is the shape of a CI checkout after `setup-venvs.sh --skip-runner`,
    and it is the case that fails today: test.sh calls require_venv on
    venv-runner unconditionally and exits 1 before running anything.

    venv-ui is symlinked to the real one rather than rebuilt -- the point is
    the absence of venv-runner, not a fresh venv.
    """
    real_ui = REPO / ".venvs" / "venv-ui"
    if not (real_ui / "bin" / "python3").exists():
        pytest.fail(
            f"{real_ui} is missing; run scripts/setup-venvs.sh before this test. "
            "This is a failure rather than a skip: a silently skipped test here "
            "is indistinguishable from a passing one."
        )
    prefix = tmp_path_factory.mktemp("ui-only-prefix")
    (prefix / "venv-ui").symlink_to(real_ui, target_is_directory=True)
    assert not (prefix / "venv-runner").exists()
    return prefix


def _run(args, prefix, timeout=900):
    env = dict(os.environ, TT_BIO_DEMO_PREFIX=str(prefix))
    return subprocess.run(
        [str(TEST_SH), *args],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout,
    )


def test_ui_only_runs_without_a_venv_runner(ui_only_prefix):
    """The regression that matters: no venv-runner must not be fatal."""
    r = _run(["--ui-only", "--collect-only", "-q"], ui_only_prefix)
    assert "venv-runner not found" not in r.stderr, r.stderr
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"


def test_ui_only_names_itself_in_the_verdict(ui_only_prefix):
    """A narrowed run has to say so where the verdict is read."""
    r = _run(["--ui-only", "--collect-only", "-q"], ui_only_prefix)
    assert "runner half: SKIPPED (--ui-only)" in r.stdout, r.stdout
    overall = [ln for ln in r.stdout.splitlines() if ln.startswith("OVERALL:")]
    assert overall, r.stdout
    assert "--ui-only" in overall[-1], overall


def test_ui_only_never_claims_both_halves_are_green(ui_only_prefix):
    """The exact sentence a full run prints must not appear."""
    r = _run(["--ui-only", "--collect-only", "-q"], ui_only_prefix)
    assert "both halves green" not in r.stdout, r.stdout


def test_ui_only_and_hw_are_rejected_together(ui_only_prefix):
    """Every hardware test lives in the half --ui-only removes."""
    r = _run(["--ui-only", "--hw", "--collect-only", "-q"], ui_only_prefix)
    assert r.returncode == 1, r.stdout
    assert "mutually exclusive" in r.stderr, r.stderr


def test_ui_only_is_documented_in_usage():
    r = subprocess.run([str(TEST_SH), "--help"],
                       cwd=REPO, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert "--ui-only" in r.stdout, r.stdout
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_test_sh_ui_only.py -v`

Expected: FAIL. `test_ui_only_runs_without_a_venv_runner` fails with `venv-runner not found` in stderr and returncode 1, because `require_venv "$VENV_RUNNER"` runs unconditionally today.

- [ ] **Step 3: Document the flag in `usage()`**

In `scripts/test.sh`, inside the `usage()` heredoc, after the paragraph ending `See the comment above HW_POOL_TEST in this script.`, insert:

```
--ui-only runs ONLY the venv-ui half, and does not require venv-runner to
exist at all. It is for CI, which builds venv-ui alone because venv-runner
means torch, ttnn and the SFPI toolchain -- gigabytes per run. The runner
half is then reported as SKIPPED on its own line and named in the OVERALL
verdict: a run that proved less than a full one must never read like a full
one. --ui-only and --hw are mutually exclusive, because every hardware test
lives in the half --ui-only removes.
```

- [ ] **Step 4: Parse the flag and reject the contradiction**

Replace the `RUN_HW=0` block (through the `set --` line) with:

```bash
RUN_HW=0
UI_ONLY=0
if [[ "${TT_BIO_DEMO_HW_TESTS:-0}" == "1" ]]; then
  RUN_HW=1
fi
_passthrough=()
for _arg in "$@"; do
  if [[ "$_arg" == "--hw" ]]; then
    RUN_HW=1
  elif [[ "$_arg" == "--ui-only" ]]; then
    UI_ONLY=1
  else
    _passthrough+=("$_arg")
  fi
done
set -- ${_passthrough[@]+"${_passthrough[@]}"}

# Contradictory rather than merely redundant: every hardware test lives in
# tests/integration, which runs in the runner half -- the half --ui-only
# removes. Honouring both would mean silently dropping the hardware coverage
# someone just asked for, which is the exact failure --hw's own comment above
# exists to prevent.
if [[ "$UI_ONLY" -eq 1 && "$RUN_HW" -eq 1 ]]; then
  echo "ERROR: --ui-only and --hw are mutually exclusive -- every hardware test lives in the runner half, which --ui-only does not run." >&2
  if [[ "${TT_BIO_DEMO_HW_TESTS:-0}" == "1" ]]; then
    echo "       (--hw was not passed on the command line; TT_BIO_DEMO_HW_TESTS=1 is set in this environment.)" >&2
  fi
  exit 1
fi
```

- [ ] **Step 5: Require `venv-runner` only when it will be used**

Replace the four `require_venv`/`require_pytest` calls with:

```bash
require_venv "$VENV_UI" "venv-ui"
require_pytest "$VENV_UI" "venv-ui" \
  "venv-ui is built --system-site-packages and should always have pytest via apt's python3-pytest. Rerun scripts/setup-venvs.sh --force to rebuild it."

# Under --ui-only these are not merely skipped checks -- venv-runner is
# expected to be ABSENT (CI runs setup-venvs.sh --skip-runner), so demanding
# it here would fail every CI run before a single test executed.
if [[ "$UI_ONLY" -eq 0 ]]; then
  require_venv "$VENV_RUNNER" "venv-runner"
  require_pytest "$VENV_RUNNER" "venv-runner" \
    "'pip install tt-bio' does not pull in pytest -- it is not one of tt-bio's own dependencies. Rerun scripts/setup-venvs.sh --dev to add pytest to venv-runner (see docs/venv-bootstrap-notes.md, '--dev, and why it's a flag, not automatic')."
fi
```

- [ ] **Step 6: Skip the runner half and the hardware block**

Wrap the hardware-note block and both runner-side `run_half` calls. The `if [[ -d "${REPO_ROOT}/tests/integration" ]]` block that sets `HW_NOTE` and the two `run_half` calls that follow it become:

```bash
if [[ "$UI_ONLY" -eq 0 ]]; then
  if [[ -d "${REPO_ROOT}/tests/integration" ]]; then
    if [[ "$RUN_HW" -eq 1 ]]; then
      RUNNER_PATHS+=(tests/integration --ignore="${HW_POOL_TEST}")
      RUN_HW_POOL=1
      HW_NOTE="INCLUDED (--hw) -- this opens every Tenstorrent card on the box"
    else
      HW_NOTE="SKIPPED -- pass --hw (or TT_BIO_DEMO_HW_TESTS=1) to run them"
    fi
    echo
    echo "hardware tests (tests/integration): ${HW_NOTE}"
  fi

  # First, so the process that spawns worker children is the freshest one
  # there is. See the comment above HW_POOL_TEST for why this cannot share a
  # process with the in-process device tests.
  if [[ "$RUN_HW_POOL" -eq 1 ]]; then
    run_half HWPOOL "${VENV_RUNNER}/bin/python3" "$HW_POOL_TEST"
  fi
  run_half RUNNER "${VENV_RUNNER}/bin/python3" "${RUNNER_PATHS[@]}"
fi
```

Keep the `HW_POOL_TEST`, `RUNNER_PATHS`, `HW_NOTE` and `RUN_HW_POOL` initialisations where they are, above this block.

- [ ] **Step 7: Make the verdict tell the truth**

In the combined-result block, replace the runner-half report with:

```bash
if [[ "$UI_ONLY" -eq 1 ]]; then
  echo "runner half: SKIPPED (--ui-only) -- venv-runner was neither required nor run"
elif [[ "$RUNNER_RC" -eq 0 ]]; then
  echo "runner half: passed   (${RUNNER_SUMMARY})"
else
  echo "runner half: FAILED (exit ${RUNNER_RC})   (${RUNNER_SUMMARY})"
  overall_rc=1
fi
```

Guard the trailing hardware line so it does not appear for a run that never considered hardware:

```bash
if [[ "$UI_ONLY" -eq 0 && -d "${REPO_ROOT}/tests/integration" ]]; then
  echo "hardware:    ${HW_NOTE}"
fi
```

And replace the `OVERALL: PASS` branch:

```bash
if [[ "$overall_rc" -eq 0 ]]; then
  if [[ "$UI_ONLY" -eq 1 ]]; then
    echo "OVERALL: PASS (UI half only -- --ui-only; the runner half did NOT run)"
  elif [[ "$RUN_HW" -eq 1 ]]; then
    echo "OVERALL: PASS (both halves green, hardware tests included)"
  else
    echo "OVERALL: PASS (both halves green, hardware tests NOT run)"
  fi
else
  echo "OVERALL: FAIL -- see the half(s) marked FAILED above"
fi
```

- [ ] **Step 8: Run the new tests to verify they pass**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_test_sh_ui_only.py -v`
Expected: 5 passed.

- [ ] **Step 9: Verify the full suite is unharmed**

Run: `./scripts/test.sh -q`
Expected: `OVERALL: PASS (both halves green, hardware tests NOT run)` — the unflagged path must be byte-for-byte the behaviour it had before.

- [ ] **Step 10: Commit**

```bash
git add scripts/test.sh tests/unit/test_test_sh_ui_only.py
git commit -m "test.sh: --ui-only, for a CI that has no venv-runner

CI builds venv-ui alone; venv-runner is torch, ttnn and SFPI. A -k selector
cannot express that, because a half matching zero tests is a failure here by
design, so this is a real flag that drops the runner half outright and does
not require the venv to exist.

It narrows what runs without weakening how the result is judged: the runner
half is reported SKIPPED on its own line, OVERALL names the flag, and the
'both halves green' sentence cannot appear. --hw is rejected alongside it,
since every hardware test lives in the half this removes."
```

---

### Task 2: The version invariant, as tests

**Files:**
- Create: `tests/unit/test_version_consistency.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: three tests that run inside the UI half, and therefore inside Task 3's gate. No importable API.

- [ ] **Step 1: Write the failing tests**

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

Runs under venv-ui. Needs no venv, no device and no package build -- which is
why it is here rather than in test_packaging.py, where every test waits on a
module-scoped dpkg-buildpackage.
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

- [ ] **Step 2: Run the tests to verify they pass, then prove they can fail**

Run: `.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_version_consistency.py -v`
Expected: 3 passed (the repo is currently consistent at `0.1.0`).

A test that has never failed is not yet a test. Prove each one bites:

```bash
# 1. changelog drift
sed -i '1s/(0.1.0)/(0.9.9)/' debian/changelog
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_version_consistency.py -x -q
# Expected: test_the_changelog_agrees_with_the_version_file FAILS
git checkout debian/changelog

# 2. a stale handout
echo "0.2.0" > VERSION
.venvs/venv-ui/bin/python3 -m pytest tests/unit/test_version_consistency.py -q
# Expected: the changelog AND handout tests both FAIL
git checkout VERSION
```

- [ ] **Step 3: Confirm the working tree is clean again**

Run: `git status --porcelain`
Expected: only `tests/unit/test_version_consistency.py` as untracked. If `VERSION` or `debian/changelog` appear, the `git checkout`s above did not run — restore them before committing.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_version_consistency.py
git commit -m "test: one version, and the four places it has to agree

VERSION, debian/changelog and the handout PDF are checked here so they fail
on a developer's machine; the git tag is checked in CI, which is the only
place a tag exists.

The PDF is why this exists. docs/onepager/build.sh stamps VERSION into the
sheet, so bumping VERSION without re-rendering ships a handout claiming the
old version -- on the artefact most likely to be printed and handed to a
stranger. pdftotext catches it for the cost of poppler-utils, and its absence
FAILS rather than skips: a silently skipped check reads exactly like a
passing one.

Own module rather than test_packaging.py, whose every test waits on a
module-scoped dpkg-buildpackage these three string comparisons do not need."
```

---

### Task 3: The workflow

**Files:**
- Create: `.github/workflows/packages.yml`

**Interfaces:**
- Consumes: `scripts/test.sh --ui-only` (Task 1); the version tests (Task 2) via the gate; the existing `scripts/setup-venvs.sh --skip-runner` and `scripts/build-deb.sh`.
- Produces: a GitHub Release on tags, carrying `dist/*.deb`, `dist/*.buildinfo`, `dist/*.changes` and `docs/tt-bio-demo-onepager.pdf`.

- [ ] **Step 1: Create the workflow**

Create `.github/workflows/packages.yml`:

```yaml
# Builds the four Debian packages on every push, and publishes them as a
# GitHub Release when the ref is a tag.
#
# Design: docs/superpowers/specs/2026-08-17-release-ci-design.md
#
# What this deliberately does NOT do:
#   - build venv-runner (torch + ttnn + SFPI, several GB per run), so the
#     runner half of the suite is NOT covered here. Known limitation, not an
#     oversight: ./scripts/test.sh with no flags is what covers it locally,
#     before you tag.
#   - touch Tenstorrent hardware, or pass --device to any container.
#   - run `tt-bio install-deps`, which installs kernel modules.
#   - publish an apt repository or sign anything. Installing still means
#     downloading the .debs from the release page.
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
      # seconds rather than after a five-minute gate. VERSION's agreement with
      # the changelog and the handout is checked by the test suite below --
      # only the tag needs CI, because only CI has one.
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

      # python3-pytest is not optional: venv-ui is built --system-site-packages
      # and takes pytest from apt rather than pip (see test.sh's require_pytest
      # message). poppler-utils backs the handout version check in
      # tests/unit/test_version_consistency.py.
      - name: Install build and UI dependencies
        run: |
          sudo apt-get update
          sudo apt-get install -y --no-install-recommends \
            debhelper devscripts dpkg-dev \
            poppler-utils \
            python3-venv python3-pip python3-pytest \
            python3-gi python3-gi-cairo gir1.2-gtk-4.0 \
            python3-gemmi python3-opengl python3-numpy \
            libgl1 libglu1-mesa

      # --skip-runner is the whole reason --ui-only exists. Default prefix
      # (.venvs/) -- CI is not installing a booth, only building one venv to
      # test with.
      - name: Build venv-ui
        run: ./scripts/setup-venvs.sh --skip-runner

      # The gate. Carries the version-consistency tests and the packaging
      # tests -- including the throwaway-container install harness, which
      # needs the Docker preinstalled on this runner and must FAIL, never
      # skip, if it is missing.
      #
      # No xvfb-run, deliberately. Measured 2026-08-17: this half passes with
      # DISPLAY and WAYLAND_DISPLAY both unset (1115 passed in 228s). If that
      # ever stops being true here, wrap this one line -- nothing else changes.
      - name: Gate — UI half, version and packaging tests
        run: ./scripts/test.sh --ui-only

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

- [ ] **Step 2: Lint the YAML before pushing it**

Run:

```bash
.venvs/venv-ui/bin/python3 -c "
import yaml, pathlib
d = yaml.safe_load(pathlib.Path('.github/workflows/packages.yml').read_text())
print('jobs:', list(d['jobs']))
assert d['jobs']['build']['runs-on'] == 'ubuntu-24.04'
assert d['jobs']['release']['permissions']['contents'] == 'write'
assert d['permissions']['contents'] == 'read'
print('ok')
"
```

Expected: `jobs: ['build', 'release']` then `ok`. If PyYAML is not importable under `venv-ui`, use `python3 -c` with the system interpreter instead — this is a syntax check, not a test.

Note: `on:` parses as the boolean key `True` in YAML 1.1. That is a quirk of the loader, not a defect in the file; do not "fix" it.

- [ ] **Step 3: Verify the gate command works end to end locally**

Run: `./scripts/test.sh --ui-only`
Expected: `OVERALL: PASS (UI half only -- --ui-only; the runner half did NOT run)`, having run the version and packaging tests. This is exactly the command the workflow runs.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/packages.yml
git commit -m "ci: build the packages on every push, publish them on a tag

One workflow. build runs everywhere and is gated on the UI half plus the
packaging and version tests; release runs on tag refs only and is the one
job with contents: write.

The tag/VERSION check runs first so a mis-tagged release dies in seconds
rather than after the gate. Release notes are the debian/changelog stanza via
dpkg-parsechangelog -- hand-written prose about this exact version, where
generated notes would publish our internal commit messages. The release job
downloads rather than rebuilds, so what was tested is what ships, and it
refuses to replace an existing release.

No xvfb: measured 2026-08-17, the UI half passes with DISPLAY and
WAYLAND_DISPLAY unset. runs-on is pinned to ubuntu-24.04 because
debian/changelog targets noble."
```

- [ ] **Step 5: Push and watch the first run**

```bash
git push origin main
gh run watch
```

Expected: the `build` job succeeds; `release` is skipped (not a tag). If the gate fails on a display error, wrap the gate step in `xvfb-run --auto-servernum` and note that the 2026-08-17 measurement did not transfer to a runner.

---

## What this plan does NOT do

Cutting the first actual release, and updating the docs afterwards. `INSTALL.md` step 1 and README's "Installing a booth machine" still tell a reader to build the packages themselves — correct until a release exists to link to, and a follow-up once one does. `VERSION` stays at `0.1.0`; tagging is a human decision, not a plan step.

---

## Self-Review

**Spec coverage.** §2 trigger/shape → Task 3 Step 1. §3 version invariant: `VERSION`↔changelog↔PDF → Task 2; tag → Task 3's first step. §4 gate and the `--ui-only` requirement → Task 1, used in Task 3. §5 jobs and permissions → Task 3. §6 failure modes: version mismatch (Task 2), tag mismatch (Task 3 step 1), stale PDF (Task 2), Docker missing (inherited — the container harness already fails rather than skips), release exists (Task 3), runner-half gap (documented in the workflow header). §7 doc consequences → deliberately excluded above. §8 no xvfb → Task 3 comment plus the fallback in Step 5.

**Placeholder scan.** No TBDs. Every code step carries the actual code; every run step carries the command and its expected output.

**Type and name consistency.** `--ui-only` is spelled identically in the usage text, the parser, the tests, and the workflow. `UI_ONLY` matches the existing `RUN_HW` convention. The verdict strings asserted in Task 1's tests (`runner half: SKIPPED (--ui-only)`, `both halves green`, `--ui-only` inside the `OVERALL:` line) are exactly the strings Steps 6–7 emit. Artifact name `packages` matches between upload and download.

**One risk carried knowingly.** Task 1's tests shell out to `test.sh --collect-only`, so pytest runs inside pytest. It is the only way to test the script's own control flow rather than a reimplementation of it, and collection of 1115 tests is seconds rather than the 228 s a real run costs.
