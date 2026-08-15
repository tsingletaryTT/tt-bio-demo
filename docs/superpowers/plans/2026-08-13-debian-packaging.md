# Debian Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `sudo apt install tt-bio-demo-all` on a freshly imaged QB2 produces a working booth.

> **STATUS: all nine tasks shipped. Audited 2026-08-15.**
>
> The checkboxes below were never maintained while the plan was being
> executed — every one of them was still unticked long after the packages
> were building — so they said nothing, and a reader could reasonably have
> taken that as "none of this happened". They were ticked in one pass on
> 2026-08-15, against evidence rather than memory:
>
> - every task's named deliverable exists in the tree (`debian/control`,
>   `rules`, `tt-bio-demo.install`, `helpers.sh`, the runtime and weights
>   `config`/`postinst`/`templates` triples, the user unit, the desktop
>   entry, `scripts/deb-container.sh`, `scripts/make-thumbnails.py`,
>   `scripts/build-deb.sh`);
> - `tests/unit/test_packaging.py` has **55 tests covering all nine**, and
>   they pass — including the ones that build all four real `.deb` files
>   with `dpkg-buildpackage` and inspect them with `dpkg-deb`.
>
> Git history is still the authority on what landed and when. This plan is a
> record of what was intended; where the two disagree, believe git.

**Architecture:** Four Debian packages per the design spec §7, built with debhelper 13 from this repo. The application package ships source and configuration; a runtime package builds the two venvs in `/opt/tt-bio-demo` by invoking the *existing* `scripts/setup-venvs.sh --prefix`; a weights package downloads model checkpoints under a debconf prompt and pre-warms the tt-metal kernel cache; a metapackage ties them together. No packaging logic is duplicated from the scripts the project already tests.

**Tech Stack:** debhelper 13, dpkg-buildpackage, debconf, systemd user units, plain POSIX shell for maintainer scripts.

## Global Constraints

- **Never install on the host; install in a disposable container.** These packages install kernel modules and system packages, and the dev box is shared and in active use — `dpkg -i` on the host is forbidden. But installs *must* be tested, so they are tested in a throwaway container that is destroyed afterwards (`--rm`). Docker is available and permitted to this user (verified 2026-08-13).

  Two tiers, and use the cheapest that answers the question:

  | Tier | Image | For |
  |---|---|---|
  | Fast | `ubuntu:24.04` (already pulled) | Per-task verification: does it install, what does postinst do, is it idempotent, does purge clean up |
  | Fidelity | `tenstorrent/qb2-env` from `~/code/tt-developer-image/docker/Dockerfile.qb2` | The real target: "a TT-QuietBox 2 immediately after first boot post-tt-installer" — user `ttuser`, Ubuntu 24.04, Blackhole, venvs at the paths tt-installer creates |

  **This makes the tests real rather than textual.** Grepping a postinst for `Default: false` proves the string exists; installing noninteractively in a container and asserting `tt-bio install-deps` did *not* run proves the behaviour. This project's signature failure is tests that cannot fail — prefer the container assertion every time one is available. Where a test can only be textual, say so and explain why.

  The container has no Tenstorrent device unless one is passed in, and **none should be**. A package that only works with silicon attached cannot be verified at install time anyway; the booth's own preflight is what covers that.
- **`tt-bio install-deps` runs only after an explicit debconf prompt that defaults to declining.** (User ruling, 2026-08-13.) The project's standing rule is that it is never run silently; a prompt is consent, an unattended install is not. A noninteractive install must decline and print exactly what the operator should run.
- **Weights are downloaded in postinst, never shipped in the package.** Spec §7: offline operation is required at the venue, not at install time.
- **tt-bio is pinned to a release tag, never `main`.** The pin already lives in one variable at the top of `scripts/setup-venvs.sh` (`TT_BIO_VERSION`); packaging must read that pin rather than introduce a second one that can drift.
- **Maintainer scripts are idempotent and fail loudly.** A half-configured package that exits 0 is worse than one that fails: `apt` will consider it installed. Reinstalling or upgrading must not corrupt an existing `/opt/tt-bio-demo`.
- **No network at the venue.** Anything the booth needs at run time must be on disk after install.
- The application runs as a `systemd --user` service, not a system service, and not a kiosk session (spec: "less display-stack surface to debug at a venue").
- Never a bare `python3`. Never `tt-smi -r`. `./scripts/test.sh` plain stays green throughout.

---

## File Structure

| File | Responsibility |
|---|---|
| `debian/control` | The four binary packages, their dependencies and descriptions |
| `debian/changelog` | Version, derived from the project's own version |
| `debian/rules` | `dh` sequence; installs source trees, unit, `.desktop` |
| `debian/copyright` | This project's licence plus the vendored tensix-viz asset |
| `debian/tt-bio-demo.install` | Which paths land in the application package |
| `debian/tt-bio-demo.service` | `systemd --user` unit for the daemon |
| `debian/tt-bio-demo.desktop` | Desktop entry for the UI |
| `debian/tt-bio-demo-runtime.{templates,config,postinst,prerm}` | Venv build, the install-deps prompt |
| `debian/tt-bio-demo-weights.{templates,config,postinst}` | Weight download, checksum verify, cache pre-warm |
| `debian/helpers.sh` | Shared shell functions, sourced by maintainer scripts — the only place with testable logic |
| `tests/unit/test_packaging.py` | Asserts against built artifacts and `helpers.sh` behaviour |

`debian/helpers.sh` exists so maintainer-script logic is testable at all. Anything with a branch in it belongs there, not inline in a postinst.

---

## Task 1: The package skeleton that builds

**Files:** Create `debian/control`, `debian/changelog`, `debian/rules`, `debian/copyright`, `debian/source/format`. Test: `tests/unit/test_packaging.py`

**Produces:** `dpkg-buildpackage -us -uc -b` produces four `.deb` files.

**Why first:** every later task needs something to install into. A skeleton that builds is the scaffold; it ships almost nothing.

- [x] **Step 1: Write the failing test**

```python
import subprocess, pathlib, pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
EXPECTED = {"tt-bio-demo", "tt-bio-demo-runtime",
            "tt-bio-demo-weights", "tt-bio-demo-all"}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Build the packages once. Skips (loudly) if debhelper is unavailable."""
    out = tmp_path_factory.mktemp("debs")
    r = subprocess.run(["dpkg-buildpackage", "-us", "-uc", "-b"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, f"build failed:\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}"
    debs = list(REPO.parent.glob("tt-bio-demo*_*.deb"))
    assert debs, "build reported success but produced no .deb"
    for d in debs:
        d.rename(out / d.name)
    return out


def test_all_four_packages_are_produced(built):
    names = {p.name.split("_")[0] for p in built.glob("*.deb")}
    assert names == EXPECTED


def test_the_metapackage_depends_on_the_other_three(built):
    deb = next(built.glob("tt-bio-demo-all_*.deb"))
    info = subprocess.run(["dpkg-deb", "--field", str(deb), "Depends"],
                          capture_output=True, text=True).stdout
    for pkg in EXPECTED - {"tt-bio-demo-all"}:
        assert pkg in info, f"metapackage does not depend on {pkg}"


def test_no_package_ships_a_venv_or_weights(built):
    """Venvs are built in postinst and weights downloaded; shipping either
    would make a multi-gigabyte .deb that also goes stale."""
    for deb in built.glob("*.deb"):
        contents = subprocess.run(["dpkg-deb", "--contents", str(deb)],
                                  capture_output=True, text=True).stdout
        assert "/venv-ui/" not in contents, f"{deb.name} ships a venv"
        assert "/venv-runner/" not in contents, f"{deb.name} ships a venv"
        assert ".safetensors" not in contents, f"{deb.name} ships weights"
        assert ".ckpt" not in contents, f"{deb.name} ships weights"
```

**Mutations these must catch:** dropping a package from `control` (test 1 red); a metapackage that depends on nothing (test 2 red); adding `.venvs/` to the install list (test 3 red).

Declared dependencies for `tt-bio-demo`, from spec §7: `python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-4.0`, `python3-gemmi`, `libgl1`, `libglu1-mesa`, `curl`, `ca-certificates`. Add `gir1.2-webkit-6.0` — the Tensix panel needs it and is fail-soft without it, so it belongs in `Recommends`, not `Depends`. `tt-installer` is `Recommends` per the spec.

`debian/changelog` must carry a real version. Derive it from the project rather than inventing one, and say in the plan file where it came from.

- [x] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

---

## Task 2: The container harness that makes install tests real

**Files:** Create `tests/unit/conftest_container.py` (or extend the existing conftest), `scripts/deb-container.sh`. Test: `tests/unit/test_packaging.py`

**Produces:** a `container` pytest fixture exposing `.install(pkg, env=, preseed=, shim=)` returning a result with `.installed`, `.log`, `.status(pkg)`, `.shim_called_with(arg)`, `.shim_call_count(arg)`, `.shim_log`.

**Why this task exists:** every later task's most important assertion is behavioural — *did the postinst actually do the right thing* — and without this harness those tests degrade into grepping shell scripts for strings. This project's recurring failure is tests that cannot fail; a text search for `Default: false` is exactly that shape. Build the harness first and the rest of the plan gets to assert on behaviour.

**How it must work:**

- Runs `docker run --rm` against `ubuntu:24.04` (already pulled locally). Never touches the host's dpkg database. Never passes `--device /dev/tenstorrent`.
- Copies the built `.deb` files in, installs with `apt-get install -y ./pkg.deb` so dependencies resolve, and returns everything the test needs to judge what happened.
- **The `shim` parameter is the load-bearing part.** It puts a fake executable of that name early on `PATH` inside the container which appends its arguments to a log file and exits 0. That is how a test proves `tt-bio install-deps` did *or did not* run, without a real tt-bio and without installing kernel modules.
- `preseed` writes debconf selections before installing, so both branches of a prompt can be exercised.
- **If Docker is unavailable, these tests must fail loudly, not skip.** A silently skipped install test is indistinguishable from a passing one, and this project already fails a whole test half that matches zero tests for exactly that reason.

- [x] **Step 1: Write the failing test**

```python
def test_the_harness_detects_a_command_that_ran(container):
    """Prove the shim mechanism works before trusting it to prove a negative."""
    r = container.run("tt-bio install-deps --yes", shim="tt-bio")
    assert r.shim_called_with("install-deps")
    assert r.shim_call_count("install-deps") == 1


def test_the_harness_detects_a_command_that_did_not_run(container):
    r = container.run("echo doing nothing", shim="tt-bio")
    assert not r.shim_called_with("install-deps")


def test_the_harness_never_passes_a_tenstorrent_device():
    s = (REPO / "scripts" / "deb-container.sh").read_text()
    assert "/dev/tenstorrent" not in s


def test_the_harness_always_removes_its_container():
    s = (REPO / "scripts" / "deb-container.sh").read_text()
    assert "--rm" in s
```

**Mutations these must catch:** a shim that never records (test 1 red); a `shim_called_with` that returns True unconditionally (test 2 red); adding a device passthrough (test 3 red).

Tests 1 and 2 are a matched pair on purpose: a harness that always reports "it ran" and one that always reports "it didn't" both pass a single-sided test. The negative assertion in Task 5 is only trustworthy because test 1 proves the mechanism can see a positive.

- [x] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

---

## Task 3: The application package ships the right files, and only those

**Files:** Create `debian/tt-bio-demo.install`, `debian/tt-bio-demo.links` if needed. Modify `debian/rules`. Test: extend `tests/unit/test_packaging.py`

**Produces:** `/opt/tt-bio-demo/{ui,runner,protocol,playlist,examples,scripts}` and nothing else.

- [x] **Step 1: Write the failing test**

```python
def _contents(deb):
    return subprocess.run(["dpkg-deb", "--contents", str(deb)],
                          capture_output=True, text=True).stdout


def test_the_app_package_ships_every_module_the_ui_imports(built):
    deb = next(built.glob("tt-bio-demo_*.deb"))
    c = _contents(deb)
    for required in ("ui/app.py", "ui/panels.py", "ui/gallery.py",
                     "ui/states.py", "ui/telemetry.py", "ui/diagnostics.py",
                     "ui/chipviz.py", "ui/viewer.py", "ui/geometry.py",
                     "ui/playlist.py", "protocol/events.py",
                     "runner/daemon.py", "playlist/manifest.yaml"):
        assert required in c, f"{required} missing from the app package"


def test_the_vendored_tensix_viz_asset_ships(built):
    """The venue is offline; a CDN reference would render an empty panel."""
    c = _contents(next(built.glob("tt-bio-demo_*.deb")))
    assert "tensix-viz.js" in c and "tensix-viz.css" in c


def test_every_playlist_input_ships(built):
    """A manifest entry whose input is missing is a booth that fails mid-loop."""
    import yaml
    c = _contents(next(built.glob("tt-bio-demo_*.deb")))
    manifest = yaml.safe_load((REPO / "playlist/manifest.yaml").read_text())
    for entry in manifest:
        name = pathlib.Path(entry["input"]).name
        assert name in c, f"{entry['id']} names an input not shipped: {name}"


def test_tests_and_scratch_do_not_ship(built):
    c = _contents(next(built.glob("tt-bio-demo_*.deb")))
    for unwanted in ("tests/", ".superpowers/", ".venvs/", "booth-demo"):
        assert unwanted not in c, f"{unwanted} should not be in the package"
```

**Mutations these must catch:** omitting `protocol/` from the install list (test 1 red); referencing tensix-viz by CDN instead of shipping it (test 2 red); adding a manifest entry whose input is not installed (test 3 red); globbing the whole repo (test 4 red).

Note test 3 is the packaging counterpart of `test_every_shipped_target_points_at_a_file_that_exists`, and it exists for the same reason: Phase 4 grows the playlist, and a content mistake should fail the build rather than the booth.

- [x] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

---

## Task 4: `helpers.sh` — the only place with testable logic

**Files:** Create `debian/helpers.sh`. Test: extend `tests/unit/test_packaging.py`

**Produces:** `tt_bio_demo_prefix`, `tt_bio_demo_pinned_version`, `tt_bio_demo_have_command`, `tt_bio_demo_verify_sha256`, `tt_bio_demo_log`.

**Why:** maintainer scripts are the least testable code in any Debian package and the most damaging when wrong. Everything with a branch goes here, where a test can call it directly.

- [x] **Step 1: Write the failing test**

```python
HELPERS = REPO / "debian" / "helpers.sh"


def _sh(script, **env):
    import os
    return subprocess.run(["sh", "-c", f". {HELPERS}\n{script}"],
                          capture_output=True, text=True,
                          env={**os.environ, **env})


def test_the_pin_is_read_from_setup_venvs_not_duplicated():
    """A second copy of the version pin is a pin that will drift."""
    r = _sh("tt_bio_demo_pinned_version")
    assert r.returncode == 0, r.stderr
    version = r.stdout.strip()
    assert version, "no version returned"
    assert version in (REPO / "scripts" / "setup-venvs.sh").read_text()


def test_checksum_verification_rejects_a_corrupt_file(tmp_path):
    import hashlib
    f = tmp_path / "w.bin"
    f.write_bytes(b"real")
    good = hashlib.sha256(b"real").hexdigest()
    assert _sh(f"tt_bio_demo_verify_sha256 {f} {good}").returncode == 0
    f.write_bytes(b"tampered")          # same name, same length category
    assert _sh(f"tt_bio_demo_verify_sha256 {f} {good}").returncode != 0


def test_checksum_verification_fails_on_a_missing_file(tmp_path):
    """A download that silently produced nothing must not verify."""
    assert _sh(f"tt_bio_demo_verify_sha256 {tmp_path}/nope deadbeef").returncode != 0


def test_helpers_are_posix_sh_not_bashisms():
    r = subprocess.run(["sh", "-n", str(HELPERS)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
```

**Mutations these must catch:** hardcoding the version instead of reading the pin (test 1 red); a checksum function that returns 0 on mismatch (test 2 red); one that returns 0 for a missing file (test 3 red).

Test 3 is the one that matters most: the failure mode is a download that produced nothing, and a verifier that treats absence as success turns that into a booth with no weights and a package marked installed.

- [x] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

---

## Task 5: The runtime package builds the venvs, and asks before touching the system

**Files:** Create `debian/tt-bio-demo-runtime.{templates,config,postinst,prerm}`. Test: extend `tests/unit/test_packaging.py`

**Produces:** postinst runs `scripts/setup-venvs.sh --prefix /opt/tt-bio-demo`, and prompts before `tt-bio install-deps`.

**The ruling this implements (user, 2026-08-13):** `tt-bio install-deps` runs **only** after an explicit debconf prompt whose default is to decline. It installs Tenstorrent system packages and kernel modules; the project's standing rule is that this never happens silently. A noninteractive install must decline and print the exact command an operator should run.

- [x] **Step 1: Write the failing test**

```python
def test_a_noninteractive_install_does_not_run_install_deps(container):
    """The behavioural version of the ruling, not a grep for a string.

    Installs the package noninteractively in a throwaway container with a
    `tt-bio` shim on PATH that records every invocation. If install-deps ran,
    the marker file exists. This is the assertion that would actually catch a
    regression; a text search only proves a default was written down.
    """
    result = container.install(
        "tt-bio-demo-runtime",
        env={"DEBIAN_FRONTEND": "noninteractive"},
        shim="tt-bio",
    )
    assert result.installed, result.log
    assert not result.shim_called_with("install-deps"), (
        "an unattended install ran tt-bio install-deps -- it must default to "
        f"declining. shim log:\n{result.shim_log}")


def test_declining_the_prompt_still_leaves_a_usable_package(container):
    """Declining is the default path, so it must not be a broken one."""
    result = container.install("tt-bio-demo-runtime", preseed={
        "tt-bio-demo-runtime/install-deps": "boolean false"})
    assert result.installed, result.log
    assert result.status("tt-bio-demo-runtime") == "install ok installed"


def test_accepting_the_prompt_runs_it_exactly_once(container):
    result = container.install("tt-bio-demo-runtime", shim="tt-bio", preseed={
        "tt-bio-demo-runtime/install-deps": "boolean true"})
    assert result.installed, result.log
    assert result.shim_call_count("install-deps") == 1, result.shim_log


def test_install_deps_defaults_to_declining():
    """Textual companion to the behavioural test above: the declared default.

    Kept because the container test could pass for the wrong reason (e.g. the
    prompt never being asked at all), and these two fail differently.
    """
    t = (REPO / "debian" / "tt-bio-demo-runtime.templates").read_text()
    block = [b for b in t.split("\n\n") if "install-deps" in b]
    assert block, "no debconf template for the install-deps prompt"
    assert "Default: false" in block[0], "the prompt must default to declining"
    assert "Type: boolean" in block[0]


def test_postinst_builds_the_venvs_via_the_tested_script():
    """Packaging must not reimplement environment setup."""
    p = (REPO / "debian" / "tt-bio-demo-runtime.postinst").read_text()
    assert "setup-venvs.sh" in p
    assert "--prefix" in p


def test_postinst_and_prerm_are_posix_sh():
    for name in ("tt-bio-demo-runtime.postinst", "tt-bio-demo-runtime.prerm"):
        r = subprocess.run(["sh", "-n", str(REPO / "debian" / name)],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{name}: {r.stderr}"


def test_postinst_is_idempotent_in_shape():
    """Reconfigure and upgrade both re-run postinst; a second run must be safe."""
    p = (REPO / "debian" / "tt-bio-demo-runtime.postinst").read_text()
    assert "set -e" in p, "a maintainer script that ignores errors reports success"
```

**Mutations these must catch:** flipping the template default to `true` (test 1 red); calling `tt-bio install-deps` directly (test 2 red); hand-rolling venv creation instead of calling the script (test 3 red).

`setup-venvs.sh` already exits `0` when no hardware is present and `2` when the runner venv builds but its stack will not import. Decide what postinst does with exit 2 — a booth machine with a broken runner stack is a real state, and `apt` will report success unless you make it not. Say what you chose and why.

- [x] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

---

## Task 6: The weights package, and the checksum that must not lie

**Files:** Create `debian/tt-bio-demo-weights.{templates,config,postinst}`. Test: extend `tests/unit/test_packaging.py`

**Produces:** postinst downloads model weights under a debconf prompt, verifies checksums, and pre-warms the tt-metal kernel cache.

- [x] **Step 1: Write the failing test**

```python
def test_the_weights_package_ships_no_weights(built):
    deb = next(built.glob("tt-bio-demo-weights_*.deb"))
    c = _contents(deb)
    size = deb.stat().st_size
    assert size < 200_000, f"weights package is {size} bytes; it should be scripts only"


def test_every_download_is_checksum_verified():
    """An unverified download is a booth that folds garbage, or nothing."""
    p = (REPO / "debian" / "tt-bio-demo-weights.postinst").read_text()
    assert "tt_bio_demo_verify_sha256" in p
    downloads = p.count("curl")
    verifies = p.count("tt_bio_demo_verify_sha256")
    assert verifies >= downloads, (
        f"{downloads} download(s) but only {verifies} verification(s)")


def test_the_prompt_explains_the_download_size():
    """An operator on a hotel connection deserves to know before it starts."""
    t = (REPO / "debian" / "tt-bio-demo-weights.templates").read_text()
    assert "Type: boolean" in t
    import re
    assert re.search(r"\d+\s?(GB|MB)", t), "the prompt must state the size"


def test_postinst_is_posix_sh():
    r = subprocess.run(["sh", "-n", str(REPO / "debian" / "tt-bio-demo-weights.postinst")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
```

**Mutations these must catch:** shipping a weight blob in the package (test 1 red); adding a `curl` without a matching verification (test 2 red); a prompt with no size (test 3 red).

You will not have real checksums or a real download URL for the OpenFold3 checkpoint. **Do not invent them.** Structure the script so the URLs and hashes live in one clearly-marked table, and make the absence loud: an unset hash must fail the install with a message naming what to fill in, never skip verification. Say in your report exactly what a maintainer must supply before this package can ship.

Measured for context: the weights download was previously estimated in tens of minutes on a good connection. State the real figure if you can find it in the repo's own docs; otherwise say it is unknown rather than guessing.

- [x] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

---

## Task 7: The systemd user unit and desktop entry

**Files:** Create `debian/tt-bio-demo.service`, `debian/tt-bio-demo.desktop`. Modify `debian/rules`. Test: extend `tests/unit/test_packaging.py`

**Produces:** the daemon runs as a supervised `systemd --user` service; the UI has a desktop entry.

- [x] **Step 1: Write the failing test**

```python
def test_the_unit_is_a_user_service_not_a_system_one(built):
    c = _contents(next(built.glob("tt-bio-demo_*.deb")))
    assert "/lib/systemd/user/" in c or "/usr/lib/systemd/user/" in c
    assert "/lib/systemd/system/" not in c


def test_the_daemon_restarts_if_it_dies():
    """The booth is unattended; a dead daemon must come back on its own."""
    u = (REPO / "debian" / "tt-bio-demo.service").read_text()
    assert "Restart=" in u
    assert "Restart=no" not in u


def test_the_unit_pins_the_log_root_and_budgets():
    """tt-metal writes gigabytes relative to CWD unless pinned; a service has
    no obvious CWD, so the unit must be explicit."""
    u = (REPO / "debian" / "tt-bio-demo.service").read_text()
    assert "--log-root" in u
    assert "--log-budget-gb" in u


def test_the_desktop_entry_is_valid_and_names_the_ui():
    d = (REPO / "debian" / "tt-bio-demo.desktop").read_text()
    assert d.startswith("[Desktop Entry]")
    for key in ("Type=Application", "Name=", "Exec="):
        assert key in d
```

**Mutations these must catch:** shipping a system unit instead of a user unit (test 1 red); `Restart=no` (test 2 red); dropping `--log-root` from the unit (test 3 red).

Test 3 is not hypothetical: this project measured tt-metal writing 13–14 MB/s into a file it had already unlinked, invisible to a directory walk, which would have exhausted a tmpfs in about 31 minutes. The unit is the one place that setting cannot be forgotten.

- [x] **Step 2: Implement, verify mutations, run `./scripts/test.sh`, commit**

---

## Task 8: Gallery thumbnails, rendered from real folds

**Files:** Create `scripts/make-thumbnails.py`. Modify `playlist/manifest.yaml`, `debian/tt-bio-demo.install`. Test: extend `tests/unit/test_playlist.py`

**Produces:** a `thumbnail` for every shipped target, generated by folding it once and rendering the result.

**Why this way:** the gallery already renders a deliberate placeholder, so this is an improvement rather than a gap. Generating thumbnails from real folds means the picture a visitor taps is the structure they will actually watch appear — and it costs no artwork.

This task is **hardware-gated**: it needs one fold per target. Everything else in this plan is build-time only.

- [x] **Step 1: Write the failing test**

```python
def test_every_target_with_a_thumbnail_points_at_a_file_that_exists():
    for t in load_playlist("playlist/manifest.yaml"):
        if t.thumbnail is not None:
            assert t.thumbnail.is_file(), f"{t.id} names a missing thumbnail"


def test_thumbnails_are_small_enough_to_ship():
    """A .deb is not an image host."""
    for t in load_playlist("playlist/manifest.yaml"):
        if t.thumbnail is not None:
            assert t.thumbnail.stat().st_size < 400_000, f"{t.id}: {t.thumbnail}"
```

Keep the existing "missing thumbnail is tolerated" behaviour exactly as it is — a target added before anyone has folded it must still work.

- [x] **Step 2: Implement, verify, run `./scripts/test.sh`, commit**

---

## Task 9: One command that builds and reports

**Files:** Create `scripts/build-deb.sh`. Test: extend `tests/unit/test_packaging.py`

**Produces:** one command that builds all four packages and prints what they contain.

- [x] **Step 1: Write the failing test**

```python
def test_the_build_script_refuses_to_install_anything():
    """This box is shared and these packages install kernel modules."""
    s = (REPO / "scripts" / "build-deb.sh").read_text()
    for forbidden in ("dpkg -i", "apt install", "apt-get install", "install-deps"):
        assert forbidden not in s, f"build script must not run: {forbidden}"


def test_the_build_script_is_posix_sh_or_bash_and_parses():
    r = subprocess.run(["bash", "-n", str(REPO / "scripts" / "build-deb.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
```

**Mutation this must catch:** adding a convenience `dpkg -i` to the build script (test 1 red).

Print, for each package: name, version, installed size, dependency list, and the file count. That output is what a reviewer reads instead of installing.

- [x] **Step 2: Implement, verify, run `./scripts/test.sh`, commit**

---

## Notes for whoever executes this

- The four-package split is the spec's, and it is load-bearing: weights change on a different cadence from code, and an operator must be able to reinstall one without the other.
- `debian/` in `tt-local-generator` is the prior art the spec points at, including its debconf `.templates`/`.config`/`.postinst` triple. Read it before writing yours; do not copy more than the pattern.
- `lintian` is **not installed** on this box. Do not install it. If a task would benefit from lintian output, run it inside the container instead — the harness from Task 2 already gives you a disposable Ubuntu where `apt-get install -y lintian` costs nothing and disappears on exit.
- The three untracked PNGs and `booth-demo-2min.mp4` in the repo root are not yours; leave them alone. `booth-demo-2min.mp4` is gitignored deliberately.
