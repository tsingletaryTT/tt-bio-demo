"""The container harness Task 2 of the debian-packaging plan exists to
build (see .superpowers/sdd/2026-08-13-debian-packaging/task-2-brief.md).

Every later task's most important assertion is behavioural -- did a
postinst actually run `tt-bio install-deps`, did declining a debconf prompt
still leave a package installed -- and without a real install to observe,
those assertions degrade into grepping a maintainer script for a string
like "Default: false", which proves a default was *written down*, not that
an unattended install *honours* it. This module gives tests a real,
disposable `apt-get install` to make that assertion against.

Design decisions, and why:

* One `docker run --rm` PER CALL, not one long-lived container reused
  across a test session. A reused container accumulates installed
  packages and debconf state across tests -- exactly the kind of leakage
  that would make a later test's negative assertion ("install-deps did
  NOT run") pass for the wrong reason, because some earlier test in the
  same container already ran it. Isolation is worth more here than the
  extra `docker run` startup cost per call. The image itself IS reused
  (ubuntu:24.04 is already pulled; nothing here pulls it again), so the
  cost paid per call is a container start, not an image fetch.

* State crosses the container boundary through a bind-mounted scratch
  directory (WORKDIR -> /work), not through `docker cp` after the fact:
  a shim writes its log to a file under /work as it runs, and this
  module's own trailer (in scripts/deb-container.sh) dumps dpkg status to
  another file under /work right before the container's single process
  exits -- both are readable from the host the instant `docker run --rm`
  returns, before the container's filesystem is discarded.

* Dependency resolution for `.install()` walks Depends between the
  *locally built* .deb files (via `dpkg-deb -f ... Depends` run on the
  HOST -- a read of the archive's control file, not a database write) so
  that installing e.g. tt-bio-demo-runtime also installs the tt-bio-demo
  it depends on, WITHOUT also pulling in unrelated siblings like
  tt-bio-demo-weights just because their .deb happens to exist on disk.
  `apt-get install ./a.deb ./b.deb` would install both regardless of
  whether either needs the other; giving apt only the debs the walk
  found keeps `.install("tt-bio-demo-runtime")` from silently also
  running tt-bio-demo-weights' postinst.

* If Docker is unavailable, `.run()`/`.install()` raise -- they do not
  call `pytest.skip()`. A skipped install test and a passing one render
  identically in a summary line; this project already treats that
  shape of test (see scripts/test.sh's zero-tests-matched check) as a
  failure in its own right, and the same judgement applies here.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DEB_CONTAINER_SH = REPO / "scripts" / "deb-container.sh"

# Cache of {"debs": tmp_path_holding_the_four_built_.deb_files}, populated at
# most once per test session by _built_debs() below. A plain module-level
# dict rather than a pytest fixture: it must be reachable from inside
# Container.install() lazily -- only the tests that actually call .install()
# should pay a dpkg-buildpackage's worth of time, not every test that merely
# asks for the `container` fixture to call .run().
_BUILD_CACHE: dict[str, pathlib.Path] = {}


class HarnessError(RuntimeError):
    """Raised (never pytest.skip()'d) when the harness cannot do its job."""


def _docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _require_docker() -> None:
    if not _docker_available():
        raise HarnessError(
            "Docker is required to run tests/unit/test_packaging.py's "
            "container-based checks, but `docker info` failed or `docker` "
            "is not on PATH. This raises rather than skipping on purpose "
            "(see tests/unit/conftest_container.py's module docstring): a "
            "silently skipped install test is indistinguishable from a "
            "passing one. Fix: install Docker and make sure this user can "
            "run it without sudo (`docker ps` should work with no error)."
        )


def _built_debs() -> pathlib.Path:
    """Build the four .deb files once per test session (memoized) and
    return the directory holding them.

    Mirrors tests/unit/test_packaging.py's own `built` fixture (same
    command, same "skip if debhelper is missing -> here, raise instead"
    posture, since a container-install test with no .deb to install is
    exactly the "cannot fail" shape this task exists to avoid). Deliberately
    a plain function, not a pytest fixture: Container.install() calls it
    directly so the build only happens for tests that actually install
    something.
    """
    if "debs" in _BUILD_CACHE:
        return _BUILD_CACHE["debs"]

    out = pathlib.Path(tempfile.mkdtemp(prefix="tt-bio-demo-built-debs-"))
    r = subprocess.run(
        ["dpkg-buildpackage", "-us", "-uc", "-b"],
        cwd=REPO, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise HarnessError(
            f"dpkg-buildpackage failed (needed to build .deb files for "
            f"container.install()):\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}"
        )
    debs = list(REPO.parent.glob("tt-bio-demo*_*.deb"))
    if not debs:
        raise HarnessError(
            "dpkg-buildpackage exited 0 but produced no .deb files"
        )
    for d in debs:
        d.rename(out / d.name)
    _BUILD_CACHE["debs"] = out
    return out


def _package_name(deb: pathlib.Path) -> str:
    r = subprocess.run(
        ["dpkg-deb", "-f", str(deb), "Package"],
        capture_output=True, text=True,
    )
    return r.stdout.strip()


def _depends_names(deb: pathlib.Path) -> list[str]:
    r = subprocess.run(
        ["dpkg-deb", "-f", str(deb), "Depends"],
        capture_output=True, text=True,
    )
    names = []
    for part in r.stdout.split(","):
        part = part.strip()
        if not part:
            continue
        # "tt-bio-demo (= 0.1.0)" -> "tt-bio-demo"; "${misc:Depends}" -> skip
        name = part.split()[0].split("(")[0].strip()
        if name and not name.startswith("$"):
            names.append(name)
    return names


def _resolve_local_deps(pkg: str, debs_dir: pathlib.Path) -> list[pathlib.Path]:
    """Walk Depends among the locally built .deb files, starting at `pkg`.

    Returns every .deb (pkg's own plus transitive local dependencies) that
    needs to be handed to apt together so a purely local dependency (one
    with no counterpart in any apt archive) resolves -- without pulling in
    unrelated sibling packages that merely happen to sit in the same build
    output directory.
    """
    by_name = {}
    for d in debs_dir.glob("*.deb"):
        by_name[_package_name(d)] = d

    if pkg not in by_name:
        raise HarnessError(
            f"no built .deb found for package {pkg!r}; built packages: "
            f"{sorted(by_name)}"
        )

    resolved: dict[str, pathlib.Path] = {}
    queue = [pkg]
    while queue:
        name = queue.pop()
        if name in resolved or name not in by_name:
            continue
        resolved[name] = by_name[name]
        for dep in _depends_names(by_name[name]):
            if dep in by_name and dep not in resolved:
                queue.append(dep)
    return list(resolved.values())


class ContainerResult:
    """What a `.run()` or `.install()` call hands back.

    Both call sites return this exact shape (see the brief: ".run(cmd,
    shim=None) -> same result shape, for exercising the harness itself"),
    so a test written against .install() and one written against .run()
    read identically.
    """

    def __init__(self, returncode: int, log: str, shim_log: str, status_text: str):
        self._returncode = returncode
        self.log = log
        self.shim_log = shim_log
        self.installed = returncode == 0
        self._status_text = status_text

    def status(self, pkg: str) -> str | None:
        """dpkg's own status line for `pkg` (e.g. "install ok installed"),
        or None if dpkg never heard of it inside that container."""
        for line in self._status_text.splitlines():
            name, _, rest = line.partition(" ")
            if name == pkg:
                return rest.strip()
        return None

    def shim_called_with(self, arg: str) -> bool:
        """True if some invocation of the shim was called with `arg` as one
        of its (space-separated) arguments."""
        return any(arg in line.split() for line in self.shim_log.splitlines())

    def shim_call_count(self, arg: str) -> int:
        """How many separate invocations of the shim had `arg` among their
        arguments -- NOT how many times `arg` appears in total."""
        return sum(1 for line in self.shim_log.splitlines() if arg in line.split())


class Container:
    """The `container` fixture's value. See module docstring for the
    per-call-container / bind-mount-for-state design."""

    def run(self, cmd: str, shim: str | None = None) -> ContainerResult:
        """Run an arbitrary shell command in a fresh disposable container.

        Exists so the shim mechanism itself (and the harness generally) can
        be exercised without needing a real .deb -- see
        test_the_harness_detects_a_command_that_ran/did_not_run.
        """
        return self._exec(cmd, shim=shim)

    def install(
        self,
        pkg: str,
        env: dict | None = None,
        preseed: dict | None = None,
        shim: str | None = None,
    ) -> ContainerResult:
        """Build (once per session) the project's .deb files, copy in `pkg`
        plus whatever local siblings its own Depends requires, and
        `apt-get install` them in a fresh disposable container."""
        debs_dir = _built_debs()
        deb_paths = _resolve_local_deps(pkg, debs_dir)
        targets = " ".join(f"/work/debs/{d.name}" for d in deb_paths)
        cmd = (
            "apt-get update -qq && "
            f"apt-get install -y --no-install-recommends {targets}"
        )
        return self._exec(cmd, shim=shim, env=env, preseed=preseed, deb_paths=deb_paths)

    def _exec(
        self,
        shell_cmd: str,
        *,
        shim: str | None = None,
        env: dict | None = None,
        preseed: dict | None = None,
        deb_paths: list[pathlib.Path] | None = None,
    ) -> ContainerResult:
        _require_docker()

        workdir = pathlib.Path(tempfile.mkdtemp(prefix="tt-bio-demo-container-"))
        try:
            (workdir / "bin").mkdir(parents=True, exist_ok=True)
            (workdir / "debs").mkdir(parents=True, exist_ok=True)

            if shim:
                shim_path = workdir / "bin" / shim
                # Appends its args to a log and exits 0 -- the load-bearing
                # mechanism (task brief): this is how a test proves a real
                # command DID or DID NOT run without a real binary of that
                # name and without installing anything it would install.
                shim_path.write_text(
                    "#!/bin/sh\n"
                    "echo \"$*\" >> /work/shim.log\n"
                    "exit 0\n"
                )
                shim_path.chmod(0o755)

            if preseed:
                lines = []
                for question, value in preseed.items():
                    owner = question.split("/", 1)[0]
                    lines.append(f"{owner} {question} {value}")
                (workdir / "preseed.cfg").write_text("\n".join(lines) + "\n")

            if deb_paths:
                for d in deb_paths:
                    shutil.copy2(d, workdir / "debs" / d.name)

            full_cmd = shell_cmd
            if preseed:
                full_cmd = "debconf-set-selections /work/preseed.cfg\n" + full_cmd

            env_args = []
            for k, v in (env or {}).items():
                env_args += ["--env", f"{k}={v}"]

            proc = subprocess.run(
                [str(DEB_CONTAINER_SH), str(workdir), *env_args, "--", full_cmd],
                capture_output=True, text=True, timeout=600,
            )

            shim_log_path = workdir / "shim.log"
            status_path = workdir / "status"
            shim_log = shim_log_path.read_text() if shim_log_path.exists() else ""
            status_text = status_path.read_text() if status_path.exists() else ""
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        return ContainerResult(
            returncode=proc.returncode,
            log=proc.stdout + proc.stderr,
            shim_log=shim_log,
            status_text=status_text,
        )


@pytest.fixture(scope="session")
def container() -> Container:
    """A Container is stateless config, not a running container -- see the
    module docstring for why nothing is started here. Docker's actual
    availability is checked lazily, inside .run()/.install(), so that it is
    the CALL that fails loudly if Docker is missing, not fixture setup for
    tests that were never going to touch Docker in the first place (e.g.
    the two textual tests in this task that read deb-container.sh's source
    and never request this fixture at all)."""
    return Container()
