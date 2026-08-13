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
