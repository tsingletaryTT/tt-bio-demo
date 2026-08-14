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


def test_the_harness_never_passes_a_tenstorrent_device():
    s = (REPO / "scripts" / "deb-container.sh").read_text()
    assert "/dev/tenstorrent" not in s


def test_the_harness_always_removes_its_container():
    s = (REPO / "scripts" / "deb-container.sh").read_text()
    assert "--rm" in s
