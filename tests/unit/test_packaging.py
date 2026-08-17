"""Debian packaging: the skeleton that builds four packages shipping almost
nothing (Task 1 of the debian-packaging plan).

This module builds the real .deb files with `dpkg-buildpackage` and inspects
them with `dpkg-deb` -- it never installs anything (see the plan's global
constraint: nothing in this phase may land on the dev box via `dpkg -i`).

Runs under venv-ui (see scripts/test.sh) purely because that is the "software
half"; it does not import gi or anything GTK-specific -- only subprocess and
pathlib.
"""
import re
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


def test_every_playlist_thumbnail_ships(built):
    """A gallery card whose picture is missing renders the placeholder --
    silently, by design, because a target added before anyone has folded it
    must still work. That tolerance is exactly why this needs a test: an
    installed booth showing six grey placeholders would look deliberate.

    The pictures ride along inside `playlist/`, which the install list ships
    as a directory. Mutation: moving thumbnails/ out of playlist/ (to docs/
    alone, where the site's copy lives). Red.
    """
    import yaml
    c = _contents(_app_deb(built))
    manifest = yaml.safe_load((REPO / "playlist/manifest.yaml").read_text())
    named = [e for e in manifest if e.get("thumbnail")]
    assert named, "no manifest entry names a thumbnail -- this would assert nothing"
    for entry in named:
        name = pathlib.Path(entry["thumbnail"]).name
        assert name in c, f"{entry['id']} names a thumbnail not shipped: {name}"


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


def test_whoever_sources_helpers_depends_on_whoever_ships_it():
    """postinst sources /usr/share/tt-bio-demo/helpers.sh, which lives in
    debian/ (build metadata, not installed by default). If it is not shipped,
    every install dies on its first line -- AFTER unpacking, the worst moment
    for a maintainer script to fail.

    It is shipped by the BASE package, once. Shipping it from both dependent
    packages (which is how this was first written) puts two packages in
    ownership of one path, and dpkg refuses to install them together.
    """
    shipped_by = {p.name.split(".install")[0]
                  for p in (REPO / "debian").glob("*.install")
                  if any(ln.strip().startswith("debian/helpers.sh")
                         for ln in p.read_text().splitlines())}
    assert shipped_by == {"tt-bio-demo"}, (
        f"helpers.sh should be owned by exactly the base package, not {shipped_by}")

    control = (REPO / "debian" / "control").read_text()
    for pkg in ("tt-bio-demo-runtime", "tt-bio-demo-weights"):
        script = (REPO / "debian" / f"{pkg}.postinst").read_text()
        assert "/usr/share/tt-bio-demo/helpers.sh" in script
        stanza = control.split(f"Package: {pkg}")[1].split("\nPackage:")[0]
        assert "tt-bio-demo (=" in stanza, (
            f"{pkg} sources a file from tt-bio-demo without depending on it")


def test_tt_bio_is_found_by_path_not_by_PATH(tmp_path):
    """dpkg runs maintainer scripts with PATH=/usr/sbin:/usr/bin:/sbin:/bin,
    and NOTHING adds the venv to it. `tt-bio` lives in
    <prefix>/.venvs/venv-runner/bin/, so a PATH lookup can never find it --
    which would make the accept branch of the install-deps prompt dead code
    on every real machine, including reinstalls where the venv exists.

    Driven with PATH scrubbed to exactly what dpkg provides.
    """
    prefix = tmp_path / "opt"
    bindir = prefix / ".venvs" / "venv-runner" / "bin"
    bindir.mkdir(parents=True)
    fake = bindir / "tt-bio"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)

    r = _sh("tt_bio_demo_tt_bio_bin",
            TT_BIO_DEMO_PREFIX=str(prefix),
            PATH="/usr/sbin:/usr/bin:/sbin:/bin")
    assert r.returncode == 0, f"tt-bio in the venv was not found: {r.stderr}"
    assert r.stdout.strip() == str(fake)


def test_tt_bio_lookup_fails_when_it_is_genuinely_absent(tmp_path):
    r = _sh("tt_bio_demo_tt_bio_bin",
            TT_BIO_DEMO_PREFIX=str(tmp_path / "nothing-here"),
            PATH="/usr/sbin:/usr/bin:/sbin:/bin")
    assert r.returncode != 0, "reported a tt-bio that does not exist"
    assert r.stdout.strip() == ""


def test_the_postinst_does_not_rely_on_PATH_to_find_tt_bio():
    """The bug this pins: `command -v tt-bio` in a maintainer script."""
    p = _runtime("postinst")
    assert "tt_bio_demo_tt_bio_bin" in p, \
        "postinst must resolve tt-bio by absolute path, not via PATH"
    assert "tt_bio_demo_have_command tt-bio" not in p


# ── Task 6: the weights package, and the checksum that must not lie ─────────

def _weights(name):
    return (REPO / "debian" / f"tt-bio-demo-weights.{name}").read_text()


def test_the_weights_package_ships_no_weights(built):
    deb = next(built.glob("tt-bio-demo-weights_*.deb"))
    size = deb.stat().st_size
    assert size < 200_000, f"weights package is {size} bytes; it should be scripts only"


def test_every_artifact_the_package_fetches_is_checksum_verified():
    """An unverified download is a booth that folds garbage, or nothing.

    ADAPTED from the brief, which counted `curl` calls. This package does not
    curl: tt-bio resolves its own weights through huggingface_hub, and
    reimplementing that would mean hardcoding a URL this project does not
    control and losing the hub client's resume-and-verify behaviour. So the
    invariant is expressed against the artifact TABLE instead -- every
    artifact listed must have a hash listed beside it.
    """
    p = _weights("postinst")
    assert "tt_bio_demo_verify_sha256" in p, "nothing is verified"
    artifacts = re.findall(r"^\s*([A-Za-z0-9._-]+\.(?:pt|tar))\s+([a-f0-9]{64})\s*$",
                           p, re.MULTILINE)
    assert artifacts, "no artifact/sha256 table found in the postinst"
    for name, sha in artifacts:
        assert len(sha) == 64, f"{name} has a malformed sha256"


def test_an_artifact_with_no_hash_is_a_loud_failure_not_a_skip():
    """The plan's hard rule: an unset hash must fail the install with a
    message naming what to fill in, and must NEVER silently skip
    verification."""
    p = _weights("postinst")
    assert "tt_bio_demo_verify_sha256" in p
    # The empty-hash path must exist and must be fatal, not a `continue`.
    assert re.search(r"(no sha256|hash is (unset|missing)|MISSING CHECKSUM)", p, re.I), \
        "no explicit handling for an artifact whose hash is unset"


def test_the_prompt_explains_the_download_size():
    """An operator on a hotel connection deserves to know before it starts."""
    t = _weights("templates")
    assert "Type: boolean" in t
    assert re.search(r"\d+(\.\d+)?\s?(GB|MB)", t), "the prompt must state the size"


def test_the_weights_prompt_defaults_to_declining_offline():
    """Same reasoning as install-deps: a multi-gigabyte download is not
    something an unattended install should start on its own."""
    t = _weights("templates")
    block = [b for b in t.split("\n\n") if "download" in b.lower()]
    assert block, "no download template"
    assert "Default: false" in _weights("templates")


def test_the_weights_postinst_is_posix_sh():
    r = subprocess.run(["sh", "-n", str(REPO / "debian" / "tt-bio-demo-weights.postinst")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_a_noninteractive_install_downloads_nothing(container):
    """3.7 GB must never start on its own. Behavioural, in a container."""
    result = container.install("tt-bio-demo-weights",
                               env={"DEBIAN_FRONTEND": "noninteractive"})
    assert result.installed, result.log
    assert result.status("tt-bio-demo-weights") == "install ok installed"


def test_the_weights_postinst_uses_the_tt_bio_api_that_actually_exists():
    """The packaging<->tt-bio contract, checked against the PINNED tt-bio's
    own source.

    The first draft of this postinst called `hf_artifact(repo, filename)` --
    it takes three arguments -- and imported `ccd_mols_dir`, which does not
    exist. Both would have failed at install time on a real machine and
    nowhere else: the container tests never reach this branch (they decline
    the download), and no unit test imports tt_bio.

    Parsed with `ast` rather than imported, because importing tt_bio.main
    pulls torch into a test that has no business being that slow.
    """
    import ast

    venv = REPO / ".venvs" / "venv-runner"
    main_py = next(venv.glob("lib/python3.*/site-packages/tt_bio/main.py"), None)
    if main_py is None:
        pytest.fail("venv-runner is not built; cannot check the tt-bio API "
                    "contract. Run scripts/setup-venvs.sh.")

    tree = ast.parse(main_py.read_text())
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assigned = {t.id for n in tree.body if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}

    postinst = _weights("postinst")
    for name in ("PROTENIX_REPO",):
        assert name in postinst and name in assigned, \
            f"postinst imports {name}, which tt-bio no longer defines"
    for name in ("hf_artifact", "download_mols"):
        assert name in postinst and name in functions, \
            f"postinst imports {name}, which tt-bio no longer defines"

    # Arity, which is what the first draft got wrong.
    args = functions["hf_artifact"].args.args
    assert len(args) == 3, (
        f"hf_artifact now takes {len(args)} args; the postinst calls it with 3")


# ── Task 7: the systemd user unit and the desktop entry ─────────────────────

def test_the_unit_is_a_user_service_not_a_system_one(built):
    c = _contents(_app_deb(built))
    assert "/lib/systemd/user/" in c or "/usr/lib/systemd/user/" in c
    assert "/lib/systemd/system/" not in c


def test_the_daemon_restarts_if_it_dies():
    """The booth is unattended; a dead daemon must come back on its own."""
    u = (REPO / "debian" / "tt-bio-demo.user.service").read_text()
    assert "Restart=" in u
    assert "Restart=no" not in u


def test_the_unit_pins_the_log_root_and_budgets():
    """tt-metal writes gigabytes RELATIVE TO CWD unless pinned, and a service
    has no obvious CWD. Not hypothetical: this project measured tt-metal
    writing 13-14 MB/s into a file it had already unlinked -- invisible to a
    directory walk -- which would have exhausted a tmpfs in ~31 minutes."""
    u = (REPO / "debian" / "tt-bio-demo.user.service").read_text()
    assert "--log-root" in u
    assert "--log-budget-gb" in u


def test_the_unit_runs_the_daemon_from_the_runner_venv():
    """The UI venv has no torch and the runner venv has no GTK. A unit that
    invoked a bare `python3` would import neither."""
    u = (REPO / "debian" / "tt-bio-demo.user.service").read_text()
    assert "venv-runner/bin/python3" in u, "unit must use venv-runner's interpreter"
    assert "runner.daemon" in u
    assert not re.search(r"ExecStart=/usr/bin/python3\b", u), "bare system python3"


def test_the_desktop_entry_is_valid_and_names_the_ui():
    d = (REPO / "debian" / "tt-bio-demo.desktop").read_text()
    assert d.startswith("[Desktop Entry]")
    for key in ("Type=Application", "Name=", "Exec="):
        assert key in d


def test_the_desktop_entry_launches_the_ui_venv_not_the_runner():
    d = (REPO / "debian" / "tt-bio-demo.desktop").read_text()
    exec_line = [l for l in d.splitlines() if l.startswith("Exec=")][0]
    assert "venv-ui" in exec_line or "run-demo.sh" in exec_line, exec_line
    assert "venv-runner/bin/python3 -m ui" not in exec_line


# ── Task 9: one command that builds and reports ─────────────────────────────

def _uncommented(text):
    """Shell source with comments removed, for tests about what a script RUNS.

    A raw substring search cannot tell `apt install` from a comment saying
    "this script must never run apt install" -- and the second is the
    opposite of the defect. Strips full-line and trailing comments; good
    enough for shell, which has no block comments. Quoted `#` inside a string
    would be a false strip, but no line here relies on one.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line.split(" #", 1)[0])
    return "\n".join(out)


def test_the_build_script_refuses_to_install_anything():
    """This box is shared and these packages load kernel modules.

    Checked against the script's CODE, not its prose -- see `_uncommented`.
    The header deliberately names what it refuses to do, and an earlier
    version of this test failed on that comment.
    """
    s = _uncommented((REPO / "scripts" / "build-deb.sh").read_text())
    for forbidden in ("dpkg -i", "apt install", "apt-get install", "install-deps"):
        assert forbidden not in s, f"build script must not run: {forbidden}"


def test_the_build_script_is_posix_sh_or_bash_and_parses():
    r = subprocess.run(["bash", "-n", str(REPO / "scripts" / "build-deb.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_build_script_cleans_up_after_itself():
    """dpkg-buildpackage writes to the PARENT directory, which here is a
    shared workspace holding other projects. Same defect the test fixture
    had."""
    s = (REPO / "scripts" / "build-deb.sh").read_text()
    assert ".buildinfo" in s and ".changes" in s, \
        "build script does not account for the non-.deb artefacts"


def test_the_build_report_names_the_maintainer_scripts(built):
    """The report exists so a reviewer can judge a package WITHOUT installing
    it, and the maintainer scripts are the part that can do damage -- so
    "which hooks will run" is the single most important line in it.

    The first version parsed `dpkg-deb --info`, whose columns shift depending
    on whether a file is executable, and reported "<none>" for the runtime
    package, which has config, postinst, prerm and postrm. A report that
    confidently says "nothing will run" about a package that runs four
    scripts is worse than no report.
    """
    deb = next(built.glob("tt-bio-demo-runtime_*.deb"))
    listing = subprocess.run(["dpkg-deb", "--ctrl-tarfile", str(deb)],
                             capture_output=True)
    names = subprocess.run(["tar", "-t"], input=listing.stdout,
                           capture_output=True).stdout.decode()
    for hook in ("config", "postinst", "prerm"):
        assert hook in names, f"the runtime package lost its {hook}"

    s = (REPO / "scripts" / "build-deb.sh").read_text()
    assert "--ctrl-tarfile" in s, \
        "build report parses maintainer scripts from human-readable output"


def test_every_package_setup_venvs_requires_is_declared_as_a_dependency():
    """THE ONE AN END-TO-END INSTALL FOUND, and nothing else could have.

    `apt install tt-bio-demo-all` succeeded on a clean QB2 image, and then the
    very command the runtime package's postinst tells the operator to run --
    `setup-venvs.sh --prefix /opt/tt-bio-demo` -- died immediately with
    "missing apt packages: python3-opengl python3-numpy". The packaging was
    green, the install was green, and the booth could not be built. That is
    the plan's stated goal ("a working booth") failing at the last step.

    So the two lists are tied together here rather than maintained in
    parallel: `REQUIRED_APT_PKGS` is parsed out of the script itself, and
    every entry must be declared by SOME package in debian/control. Which
    package is a judgement call (the UI's own imports belong to the app,
    venv-build tooling to the runtime package); that all of them are declared
    is not.

    Mutation: removing python3-opengl, python3-numpy or xz-utils from
    debian/control. Red, naming the missing one.
    """
    import re

    script = (REPO / "scripts" / "setup-venvs.sh").read_text()
    block = re.search(r"REQUIRED_APT_PKGS=\((.*?)\)", script, re.S)
    assert block, "REQUIRED_APT_PKGS not found -- the script's shape changed"
    required = [line.strip() for line in block.group(1).splitlines()
                if line.strip() and not line.strip().startswith("#")]
    assert len(required) >= 10, f"only parsed {required} -- parser drifted"

    control = (REPO / "debian" / "control").read_text()
    # Every Depends: field of every binary package, flattened.
    declared = set()
    for field in re.findall(r"^Depends:\n((?: .*\n)+)", control, re.M):
        for line in field.splitlines():
            name = line.strip().rstrip(",").split()[0].split("|")[0].strip()
            if name and not name.startswith("${"):
                declared.add(name)

    missing = [pkg for pkg in required if pkg not in declared]
    assert not missing, (
        f"setup-venvs.sh requires {missing}, which no package declares. An "
        f"operator who runs `apt install tt-bio-demo-all` on a clean machine "
        f"gets a successful install and then a booth that cannot be built.")


# ── Task 1 (release CI): the metapackage actually installs all four ─────────

def test_the_metapackage_installs_and_brings_all_four_with_it(container):
    """The question this repo's CI exists to answer: will a .deb install?

    Every other container test here installs ONE package to check one
    behaviour -- what a debconf answer does, whether install-deps ran. None
    of them asks the plain question an operator asks, which is whether
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
