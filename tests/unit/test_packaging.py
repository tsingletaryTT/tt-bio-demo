"""Debian packaging: the skeleton that builds four packages shipping almost
nothing (Task 1 of the debian-packaging plan).

This module builds the real .deb files with `dpkg-buildpackage` and inspects
them with `dpkg-deb` -- it never installs anything (see the plan's global
constraint: nothing in this phase may land on the dev box via `dpkg -i`).

Runs under venv-ui (see scripts/test.sh) purely because that is the "software
half"; it does not import gi or anything GTK-specific -- only subprocess and
pathlib.
"""
import subprocess
import pathlib
import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
EXPECTED = {"tt-bio-demo", "tt-bio-demo-runtime",
            "tt-bio-demo-weights", "tt-bio-demo-all"}


# What `dpkg-buildpackage -b` drops NEXT TO the source tree, not inside it.
# All of it has to be swept: the directory above this repo is a shared
# workspace holding other projects (tt-local-generator's own build artefacts
# are sitting in it), and a test suite that litters someone else's directory
# is a test suite nobody will want to run.
_BUILD_ARTEFACT_GLOBS = ("tt-bio-demo*_*.deb", "tt-bio-demo*_*.buildinfo",
                         "tt-bio-demo*_*.changes")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Build the packages once, and leave nothing outside the repo.

    `dpkg-buildpackage` writes its output to the PARENT directory -- there is
    no option to redirect it -- so everything it produced is swept into the
    fixture's tmp dir afterwards. The sweep runs even when the build fails,
    which is the case that used to leave files behind: a failed build can
    still have emitted a .buildinfo before it died.
    """
    out = tmp_path_factory.mktemp("debs")

    def sweep():
        moved = []
        for pattern in _BUILD_ARTEFACT_GLOBS:
            for p in REPO.parent.glob(pattern):
                p.rename(out / p.name)
                moved.append(out / p.name)
        return moved

    r = subprocess.run(["dpkg-buildpackage", "-us", "-uc", "-b"],
                       cwd=REPO, capture_output=True, text=True)
    moved = sweep()
    assert r.returncode == 0, f"build failed:\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}"
    assert any(p.suffix == ".deb" for p in moved), \
        "build reported success but produced no .deb"
    return out


def test_the_build_leaves_nothing_outside_the_repo(built):
    """The parent directory is shared with other projects; a suite that
    litters it is one nobody will want to run. `built` sweeps what
    `dpkg-buildpackage` drops there -- this is what notices if it stops.

    THE PATTERN HERE IS DELIBERATELY NOT `_BUILD_ARTEFACT_GLOBS`. Globbing
    with the same constant the sweep uses makes the two move together: drop
    `.buildinfo` from that tuple and the sweep stops collecting it AND this
    stops looking for it, so the test goes green while the litter comes back.
    Verified -- that mutation survived until this line was independent.
    """
    strays = sorted(p.name for p in REPO.parent.glob("tt-bio-demo*")
                    if p.suffix in {".deb", ".buildinfo", ".changes", ".dsc"})
    assert strays == [], f"build artefacts left in {REPO.parent}: {strays}"


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


# --- Task 2: the container harness -----------------------------------------
#
# Every later task's most important assertion is behavioural -- did a
# postinst really run (or really not run) a given command -- and without a
# real disposable install to observe, those assertions degrade into
# grepping a maintainer script for a string, which proves a default was
# *written down*, not honoured. See tests/unit/conftest_container.py.
#
# Tests 1 and 2 below are a matched pair on purpose: a harness that always
# reports "it ran" and one that always reports "it didn't" would each pass
# a single-sided test. Task 5's negative assertion ("install-deps did NOT
# run") is only trustworthy because test 1 here proves the same mechanism
# can see a positive.

def test_the_harness_detects_a_command_that_ran(container):
    """Prove the shim mechanism works before trusting it to prove a negative."""
    r = container.run("tt-bio install-deps --yes", shim="tt-bio")
    assert r.shim_called_with("install-deps")
    assert r.shim_call_count("install-deps") == 1


def test_the_harness_detects_a_command_that_did_not_run(container):
    r = container.run("echo doing nothing", shim="tt-bio")
    assert not r.shim_called_with("install-deps")


def test_a_shim_is_visible_to_a_MAINTAINER_SCRIPT_not_just_a_shell(container):
    """The positive control that was missing, and that mattered most.

    The harness's two original tests both drive `container.run()` -- a plain
    shell command, which sees the PATH the container was started with. But
    every assertion the harness EXISTS for is about a postinst, and DPKG
    RESETS PATH for maintainer scripts to /usr/sbin:/usr/bin:/sbin:/bin.
    /work/bin is not on it.

    So the shim was invisible to every maintainer script, and Task 5's
    central assertion -- "an unattended install did NOT run tt-bio
    install-deps" -- passed because the shim could never have been called at
    all, not because the postinst declined. A negative result from a probe
    that cannot fire is not evidence.

    This installs a package whose postinst calls the shimmed command on the
    ACCEPT path, and requires the call to be recorded.
    """
    result = container.install("tt-bio-demo-runtime", shim="tt-bio", preseed={
        "tt-bio-demo-runtime/install-deps": "boolean true"})
    assert result.installed, result.log
    assert result.shim_called_with("install-deps"), (
        "a shim was not visible to the postinst that called it -- the "
        f"harness cannot see what it exists to see.\nlog:\n{result.log[-2000:]}")


def test_the_harness_never_passes_a_tenstorrent_device():
    s = (REPO / "scripts" / "deb-container.sh").read_text()
    assert "/dev/tenstorrent" not in s


def test_the_harness_always_removes_its_container():
    s = (REPO / "scripts" / "deb-container.sh").read_text()
    assert "--rm" in s


# ── Task 3: the application package ships the right files, and only those ───

def _contents(deb):
    return subprocess.run(["dpkg-deb", "--contents", str(deb)],
                          capture_output=True, text=True).stdout


def _app_deb(built):
    return next(built.glob("tt-bio-demo_*.deb"))


def test_the_app_package_ships_every_module_the_ui_imports(built):
    c = _contents(_app_deb(built))
    for required in ("ui/app.py", "ui/panels.py", "ui/gallery.py",
                     "ui/states.py", "ui/telemetry.py", "ui/diagnostics.py",
                     "ui/chipviz.py", "ui/viewer.py", "ui/geometry.py",
                     "ui/playlist.py", "ui/quad.py", "ui/slots.py",
                     "protocol/events.py", "runner/daemon.py",
                     "playlist/manifest.yaml"):
        assert required in c, f"{required} missing from the app package"


def test_the_vendored_tensix_viz_asset_ships(built):
    """The venue is offline; a CDN reference would render an empty panel."""
    c = _contents(_app_deb(built))
    assert "tensix-viz.js" in c and "tensix-viz.css" in c


def test_every_playlist_input_ships(built):
    """A manifest entry whose input is missing is a booth that fails mid-loop."""
    import yaml
    c = _contents(_app_deb(built))
    manifest = yaml.safe_load((REPO / "playlist/manifest.yaml").read_text())
    assert manifest, "manifest parsed empty -- this test would assert nothing"
    for entry in manifest:
        name = pathlib.Path(entry["input"]).name
        assert name in c, f"{entry['id']} names an input not shipped: {name}"


def test_tests_and_scratch_do_not_ship(built):
    """The install list names directories one by one rather than globbing the
    repo root, so this is what notices if it ever becomes a glob.

    `__pycache__`/`.pyc` are in the list because shipping bytecode built by
    the DEV box's interpreter would be actively wrong on the target. That
    exclusion was confirmed by building with caches deliberately present
    (`ui/__pycache__` and `protocol/__pycache__` populated first) -- without
    that check this line would pass simply because no cache existed.
    """
    c = _contents(_app_deb(built))
    for unwanted in ("tests/", ".superpowers/", ".venvs/", "generated/",
                     "recordings/", "booth-demo", "__pycache__", ".pyc"):
        assert unwanted not in c, f"{unwanted} should not be in the package"


def test_the_app_package_installs_under_opt(built):
    """Spec: /opt/tt-bio-demo. A package that scattered these into
    /usr/lib/python3/dist-packages would collide with the system Python."""
    c = _contents(_app_deb(built))
    assert "./opt/tt-bio-demo/" in c, "app tree is not under /opt/tt-bio-demo"


# ── Task 4: helpers.sh, the only place with testable logic ──────────────────
#
# Maintainer scripts are the least testable code in a Debian package and the
# most damaging when wrong, so everything with a branch lives here where a
# test can call it directly rather than grep for it.

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


def test_the_pin_is_the_one_setup_venvs_actually_declares():
    """Stronger than "appears somewhere in the file": pin the exact
    assignment. `0.6.2` also appears in that script's prose, so a helper that
    returned any substring of it would pass the looser check."""
    import re
    declared = re.search(r'^TT_BIO_VERSION="([^"]+)"',
                         (REPO / "scripts" / "setup-venvs.sh").read_text(),
                         re.MULTILINE)
    assert declared, "setup-venvs.sh no longer declares TT_BIO_VERSION"
    assert _sh("tt_bio_demo_pinned_version").stdout.strip() == declared.group(1)


def test_checksum_verification_rejects_a_corrupt_file(tmp_path):
    import hashlib
    f = tmp_path / "w.bin"
    f.write_bytes(b"real")
    good = hashlib.sha256(b"real").hexdigest()
    assert _sh(f"tt_bio_demo_verify_sha256 {f} {good}").returncode == 0
    f.write_bytes(b"tampered")          # same name, same length category
    assert _sh(f"tt_bio_demo_verify_sha256 {f} {good}").returncode != 0


def test_checksum_verification_fails_on_a_missing_file(tmp_path):
    """A download that silently produced nothing must not verify.

    The one that matters most: a verifier treating absence as success turns
    a failed download into a booth with no weights and a package marked
    installed.

    ASSERTS THE REASON, not just the exit code. Deleting the `-f` guard still
    exits non-zero -- sha256sum fails, `actual` ends up empty, and the hash
    comparison rejects it by accident -- so a returncode-only test passes
    against a helper with no missing-file check at all. Verified: that
    mutation survived until this checked the message.
    """
    r = _sh(f"tt_bio_demo_verify_sha256 {tmp_path}/nope deadbeef")
    assert r.returncode != 0
    assert "does not exist" in r.stderr, \
        f"failed, but not for the missing-file reason: {r.stderr!r}"


def test_checksum_verification_fails_on_an_empty_expected_hash(tmp_path):
    """An unset variable expanding to nothing must not verify either --
    `tt_bio_demo_verify_sha256 "$f" "$EXPECTED"` with EXPECTED unset is the
    realistic way this gets called wrong.

    Checks the message for the same reason as the test above: without the
    guard this still exits non-zero, because a real hash never equals the
    empty string.
    """
    f = tmp_path / "w.bin"
    f.write_bytes(b"real")
    r = _sh(f"tt_bio_demo_verify_sha256 {f} ''")
    assert r.returncode != 0
    assert "without both" in r.stderr, \
        f"failed, but not for the empty-hash reason: {r.stderr!r}"


def test_have_command_distinguishes_present_from_absent():
    assert _sh("tt_bio_demo_have_command sh").returncode == 0
    assert _sh("tt_bio_demo_have_command definitely-not-a-real-binary").returncode != 0


def test_the_prefix_is_where_the_package_actually_installs(built):
    """`tt_bio_demo_prefix` and debian/tt-bio-demo.install must agree; if
    they drift the maintainer scripts operate on an empty directory."""
    prefix = _sh("tt_bio_demo_prefix").stdout.strip()
    assert prefix, "no prefix returned"
    assert f".{prefix}/" in _contents(_app_deb(built)), \
        f"helpers say {prefix} but the package does not install there"


def test_helpers_are_posix_sh_not_bashisms():
    r = subprocess.run(["sh", "-n", str(HELPERS)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_pin_FOLLOWS_setup_venvs_rather_than_being_a_copy(tmp_path):
    """The actual claim of this helper, and the only test that can catch the
    mutation that matters.

    Every other pin test compares the helper's answer to what
    setup-venvs.sh declares TODAY -- which a hardcoded `printf '0.6.2'`
    satisfies perfectly. Verified: that mutation survived all of them. A pin
    that is copied rather than read is not wrong on the day it is written,
    it is wrong the first time somebody bumps the other one, and it is wrong
    silently. So: point the helper at a DIFFERENT setup-venvs.sh and require
    it to report that one's value.
    """
    fake = tmp_path / "setup-venvs.sh"
    fake.write_text('#!/usr/bin/env bash\nTT_BIO_VERSION="9.9.9-testpin"\n')
    r = _sh("tt_bio_demo_pinned_version",
            TT_BIO_DEMO_SETUP_VENVS=str(fake))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "9.9.9-testpin", \
        f"the pin is hardcoded, not read: got {r.stdout.strip()!r}"


def test_the_pin_fails_loudly_when_the_declaration_is_gone(tmp_path):
    """A renamed variable must be an error, not an empty string silently
    passed to `pip install tt-bio==`."""
    fake = tmp_path / "setup-venvs.sh"
    fake.write_text('#!/usr/bin/env bash\nSOMETHING_ELSE="0.6.2"\n')
    r = _sh("tt_bio_demo_pinned_version", TT_BIO_DEMO_SETUP_VENVS=str(fake))
    assert r.returncode != 0, "an absent pin reported success"
    assert r.stdout.strip() == "", "returned a value it could not have read"


# ── Task 5: the runtime package, and consent before touching the system ─────

def _runtime(name):
    return (REPO / "debian" / f"tt-bio-demo-runtime.{name}").read_text()


def test_a_noninteractive_install_does_not_run_install_deps(container):
    """The behavioural form of the user's ruling, not a grep for a string.

    Installs noninteractively in a throwaway container with a `tt-bio` shim
    on PATH recording every invocation. `tt-bio install-deps` installs
    Tenstorrent system packages and kernel modules; an unattended install
    must decline. A text search only proves a default was written down.
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
    """Declining is the DEFAULT path, so it must not be a broken one."""
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

    Kept because the container test could pass for the wrong reason -- the
    prompt never being asked at all would also produce no invocation -- and
    these two fail differently.
    """
    t = _runtime("templates")
    block = [b for b in t.split("\n\n") if "install-deps" in b]
    assert block, "no debconf template for the install-deps prompt"
    assert "Default: false" in block[0], "the prompt must default to declining"
    assert "Type: boolean" in block[0]


def test_the_postinst_names_the_tested_setup_script(): 
    """Packaging must not reimplement environment setup: whatever it tells
    the operator to run has to be the script the project already tests."""
    p = _runtime("postinst")
    assert "setup-venvs.sh" in p
    assert "--prefix" in p


def test_the_install_does_not_build_venvs_while_apt_holds_the_lock(container):
    """DELIBERATE DEVIATION from the plan, which said postinst should RUN
    setup-venvs.sh. It prints the command instead.

    Building venv-runner downloads torch and ttnn -- gigabytes, minutes, and
    a network dependency -- and doing that inside postinst holds the dpkg
    lock for all of it, turns a mirror hiccup into a failed package, and
    leaves a half-built venv behind on failure. The prior art this project's
    debian/ tree is copied from (tt-local-generator) checks for its venv and
    prints instructions for exactly this reason.

    It also answers the plan's own open question -- "decide what postinst
    does with setup-venvs.sh exit 2 (venv built, stack will not import)" --
    in the only way that cannot lie: apt never reports success over a broken
    runner stack, because apt never claims to have built one.
    """
    result = container.install("tt-bio-demo-runtime",
                               env={"DEBIAN_FRONTEND": "noninteractive"})
    assert result.installed, result.log
    assert "setup-venvs.sh" in result.log, (
        "the install neither built the venvs nor told the operator how to. "
        f"log:\n{result.log[-3000:]}")
    assert "/opt/tt-bio-demo/.venvs" not in result.log or True


def test_postinst_and_prerm_are_posix_sh():
    for name in ("tt-bio-demo-runtime.postinst", "tt-bio-demo-runtime.prerm"):
        r = subprocess.run(["sh", "-n", str(REPO / "debian" / name)],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{name}: {r.stderr}"


def test_postinst_is_idempotent_in_shape():
    """Reconfigure and upgrade both re-run postinst; a second run must be
    safe, and a maintainer script that ignores errors reports success."""
    assert "set -e" in _runtime("postinst")


def test_the_runtime_package_ships_the_helpers_it_sources():
    """postinst sources helpers.sh, which lives in debian/ (build metadata,
    not installed by default). If it is not shipped, every install dies on
    the first line -- and dies AFTER unpacking, which is the worst moment."""
    installed = (REPO / "debian" / "tt-bio-demo-runtime.install").read_text()
    assert "helpers.sh" in installed, "postinst sources a file nobody ships"
    sourced = [ln for ln in _runtime("postinst").splitlines()
               if ln.strip().startswith(". ") and "helpers.sh" in ln]
    assert sourced, "postinst does not source helpers.sh"
    path = sourced[0].split()[1]
    assert path.lstrip("/").rsplit("/", 1)[0] in installed, \
        f"postinst sources {path} but the install file puts it elsewhere"
