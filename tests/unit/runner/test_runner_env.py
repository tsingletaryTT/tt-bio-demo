import os
import time
from pathlib import Path

from runner.env import INSPECTOR_VAR, LOG_ROOT_VAR, log_root_size, prune_log_root, runner_environ


def test_log_root_var_is_pinned_to_the_verified_variable_name():
    """LOG_ROOT_VAR must be TT_METAL_LOGS_PATH, not TT_METAL_INSPECTOR_LOG_PATH
    -- the name the Phase 3a plan's draft code originally used, and one this
    module's own docstring documents as fictional on this tt-metal build
    (verified via `strings` against the installed libtt_metal.so: the string
    does not appear anywhere in it, and setting it measurably had zero
    effect). Every other test in this file imports LOG_ROOT_VAR from the
    module under test and asserts values *against* it, so reverting the
    constant back to the fictional name would leave every one of them green
    -- they'd all still pass, just pinning the wrong variable. This is a
    literal string specifically so that mutation cannot hide behind it.
    """
    assert LOG_ROOT_VAR == "TT_METAL_LOGS_PATH"


def test_inspector_var_is_pinned_to_the_verified_variable_name():
    """Same hole as the LOG_ROOT_VAR test above, for INSPECTOR_VAR -- this is
    the variable that bounds the tmpfs OOM this phase's headline finding was
    about (see the module docstring's "UPDATE, Task 10" section):
    mesh_workloads_log.yaml grows unbounded while the daemon runs regardless
    of pruning, and disabling Inspector is the only thing that actually stops
    it. A test that only compared against the module's own constant would
    not catch that constant silently reverting to something inert.
    """
    assert INSPECTOR_VAR == "TT_METAL_INSPECTOR"


def test_inspector_log_path_is_absolute_and_under_the_log_root(tmp_path):
    env = runner_environ(tmp_path / "logs", base={})
    value = env[LOG_ROOT_VAR]
    assert os.path.isabs(value), f"{LOG_ROOT_VAR} must be absolute, got {value!r}"
    assert str(tmp_path / "logs") in value


def test_relative_log_root_is_resolved_to_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = runner_environ("logs", base={})
    assert os.path.isabs(env[LOG_ROOT_VAR])


def test_base_environment_is_not_mutated():
    base = {"PATH": "/usr/bin"}
    runner_environ("/tmp/logs", base=base)
    assert base == {"PATH": "/usr/bin"}, "runner_environ must not mutate its input"


def test_base_environment_is_carried_through():
    env = runner_environ("/tmp/logs", base={"PATH": "/usr/bin", "HOME": "/home/x"})
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/x"


def test_caller_supplied_log_path_is_not_silently_overridden():
    # An operator who sets this deliberately should win; we only fill the gap.
    env = runner_environ("/tmp/logs", base={LOG_ROOT_VAR: "/operator/choice"})
    assert env[LOG_ROOT_VAR] == "/operator/choice"


def test_defaults_to_the_process_environment_when_no_base_given(monkeypatch):
    monkeypatch.setenv("TTBIO_DEMO_MARKER", "present")
    env = runner_environ("/tmp/logs")
    assert env["TTBIO_DEMO_MARKER"] == "present"


def test_inspector_is_disabled_by_default():
    """Task 10 found that tt-metal's Inspector opens
    generated/inspector/mesh_workloads_log.yaml once at device bring-up and
    holds it open (appending) for the daemon's entire life -- unlinking it
    later does not free the space while that fd stays open, so
    prune_log_root cannot actually bound it. Disabling Inspector removes the
    file entirely; nothing here reads its output.
    """
    env = runner_environ("/tmp/logs", base={})
    assert env[INSPECTOR_VAR] == "0"


def test_caller_supplied_inspector_setting_is_not_silently_overridden():
    # Same setdefault discipline as LOG_ROOT_VAR: an operator who deliberately
    # wants Inspector output for debugging keeps that choice.
    env = runner_environ("/tmp/logs", base={INSPECTOR_VAR: "1"})
    assert env[INSPECTOR_VAR] == "1"


def _file(root, name, size, age_s=0):
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_bytes(b"x" * size)
    if age_s:
        old = time.time() - age_s
        os.utime(p, (old, old))
    return p


def test_size_of_a_missing_root_is_zero(tmp_path):
    assert log_root_size(tmp_path / "nope") == 0


def test_size_counts_files_in_subdirectories(tmp_path):
    _file(tmp_path / "inspector", "a.yaml", 1000)
    _file(tmp_path / "inspector" / "deep", "b.yaml", 500)
    assert log_root_size(tmp_path) == 1500


def test_nothing_is_removed_when_under_budget(tmp_path):
    _file(tmp_path, "a.yaml", 100)
    freed, removed = prune_log_root(tmp_path, max_bytes=10_000)
    assert freed == 0 and removed == []
    assert (tmp_path / "a.yaml").exists()


def test_oldest_files_go_first_until_under_budget(tmp_path):
    _file(tmp_path, "old.yaml", 1000, age_s=900)
    _file(tmp_path, "mid.yaml", 1000, age_s=600)
    _file(tmp_path, "new.yaml", 1000, age_s=1)
    freed, removed = prune_log_root(tmp_path, max_bytes=1500)
    assert not (tmp_path / "old.yaml").exists()
    assert not (tmp_path / "mid.yaml").exists()
    assert (tmp_path / "new.yaml").exists(), "the newest log must survive"
    assert freed == 2000
    assert sorted(os.path.basename(p) for p in removed) == ["mid.yaml", "old.yaml"]


def test_dry_run_reports_without_deleting(tmp_path):
    _file(tmp_path, "old.yaml", 1000, age_s=900)
    _file(tmp_path, "new.yaml", 1000, age_s=1)
    freed, removed = prune_log_root(tmp_path, max_bytes=1500, dry_run=True)
    assert freed == 1000
    assert len(removed) == 1
    assert (tmp_path / "old.yaml").exists(), "dry run must not delete"


def test_the_root_directory_itself_is_never_removed(tmp_path):
    _file(tmp_path, "a.yaml", 5000)
    prune_log_root(tmp_path, max_bytes=0)
    assert tmp_path.is_dir()


def test_a_missing_root_is_not_an_error(tmp_path):
    freed, removed = prune_log_root(tmp_path / "nope", max_bytes=100)
    assert freed == 0 and removed == []


def test_a_symlink_pointing_outside_the_root_is_never_followed(tmp_path):
    """The one that matters: this function deletes files."""
    outside = tmp_path / "precious"
    outside.mkdir()
    victim = outside / "do-not-delete.txt"
    victim.write_bytes(b"y" * 5000)

    root = tmp_path / "logs"
    root.mkdir()
    _file(root, "a.yaml", 100)
    (root / "escape").symlink_to(outside)

    prune_log_root(root, max_bytes=0)
    assert victim.exists(), "pruning escaped the log root via a symlink"


def test_refuses_a_root_that_is_not_a_directory(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    freed, removed = prune_log_root(f, max_bytes=0)
    assert freed == 0 and removed == []


def test_a_protected_path_survives_even_when_it_is_the_oldest(tmp_path):
    """Added for runner/daemon.py's structures budget (Task 10 review): a
    .cif the UI may not have read yet must never be deleted just because
    oldest-first ordering would otherwise pick it first -- pruning must
    skip a protected entry and move on to the next candidate rather than
    stopping.

    Protecting the *oldest* file (rather than the newest, which is the
    real daemon's actual usage -- see the "recent path" test below) is
    deliberately the harder case: since prune_log_root still needs to
    reach budget without touching `old`, it has no choice but to delete
    `new` too, even though `new` is younger than `old`. That is the
    correct, if initially surprising, consequence of "protected" meaning
    "never delete this one," not "this one is exempt from being the reason
    something younger gets deleted instead."
    """
    old = _file(tmp_path, "old.yaml", 1000, age_s=900)
    _file(tmp_path, "mid.yaml", 1000, age_s=600)
    _file(tmp_path, "new.yaml", 1000, age_s=1)
    freed, removed = prune_log_root(tmp_path, max_bytes=1500, protect={str(old)})
    assert old.exists(), "a protected path must survive regardless of age"
    assert freed == 2000
    assert sorted(os.path.basename(p) for p in removed) == ["mid.yaml", "new.yaml"]


def test_a_protected_recent_path_survives_a_tight_budget(tmp_path):
    """The daemon's actual usage: the protected path is the *newest* one
    (the structure just emitted). Here oldest-first ordering would never
    have picked it anyway before hitting budget -- so protecting it and
    protecting nothing should behave identically, which is the case this
    exercises directly.
    """
    _file(tmp_path, "old.yaml", 1000, age_s=900)
    _file(tmp_path, "mid.yaml", 1000, age_s=600)
    new = _file(tmp_path, "new.yaml", 1000, age_s=1)
    freed, removed = prune_log_root(tmp_path, max_bytes=1000, protect={str(new)})
    assert new.exists(), "the protected (most recent) path must survive"
    assert not (tmp_path / "old.yaml").exists()
    assert not (tmp_path / "mid.yaml").exists()
    assert freed == 2000
    assert sorted(os.path.basename(p) for p in removed) == ["mid.yaml", "old.yaml"]


def test_protecting_everything_can_leave_the_root_over_budget(tmp_path):
    """The documented tradeoff: protection is a correctness floor ("never
    delete this"), not a budget guarantee. If the protected set alone
    exceeds max_bytes, prune_log_root must leave the root over budget
    rather than deleting something it was told to keep.
    """
    a = _file(tmp_path, "a.yaml", 1000, age_s=900)
    b = _file(tmp_path, "b.yaml", 1000, age_s=1)
    freed, removed = prune_log_root(
        tmp_path, max_bytes=500, protect={str(a), str(b)})
    assert freed == 0 and removed == []
    assert a.exists() and b.exists()


def test_protecting_some_files_still_prunes_the_rest_toward_budget(tmp_path):
    """A budget that is reachable once the protected files are set aside
    must still get reached -- protection should not make pruning give up
    on the files it's actually allowed to delete.
    """
    protected = _file(tmp_path, "keep.yaml", 1000, age_s=1)
    _file(tmp_path, "old.yaml", 1000, age_s=900)
    _file(tmp_path, "mid.yaml", 1000, age_s=600)
    freed, removed = prune_log_root(
        tmp_path, max_bytes=1000, protect={str(protected)})
    assert protected.exists()
    assert not (tmp_path / "old.yaml").exists()
    assert not (tmp_path / "mid.yaml").exists()
    assert freed == 2000
    assert sorted(os.path.basename(p) for p in removed) == ["mid.yaml", "old.yaml"]


def test_no_protect_argument_behaves_exactly_as_before(tmp_path):
    """The default (protect=None) must be a true no-op -- prune_log_root's
    pre-existing callers (the tt-metal log root) pass no protect argument
    at all and must see identical behavior to before this parameter existed.
    """
    _file(tmp_path, "old.yaml", 1000, age_s=900)
    _file(tmp_path, "new.yaml", 1000, age_s=1)
    freed, removed = prune_log_root(tmp_path, max_bytes=1500)
    assert freed == 1000
    assert removed == [str(tmp_path / "old.yaml")]


def test_a_budget_the_protected_floor_exceeds_says_so(tmp_path, caplog):
    """An over-budget root where everything is protected must not look
    exactly like a healthy sweep.

    `prune_log_root` returns `removed=[]` in both cases and the daemon's
    `if removed:` gate means an operator who sets a budget below the
    protected floor sees the same silence as a run with nothing to do
    (docs/followups.md, from Phase 3a). The floor is deliberate -- never
    delete a file a caller asked to keep -- so the fix is a signal, not a
    behaviour change.

    Mutation: dropping the `elif` branch in prune_log_root. Red.
    """
    import logging

    for name in ("a.log", "b.log"):
        (tmp_path / name).write_bytes(b"x" * 1000)
    protect = [str(tmp_path / "a.log"), str(tmp_path / "b.log")]

    with caplog.at_level(logging.WARNING, logger="runner.env"):
        freed, removed = prune_log_root(tmp_path, max_bytes=100,
                                        protect=protect)

    assert (freed, removed) == (0, []), "protected files must survive"
    assert any("every file over it is protected" in r.getMessage()
               for r in caplog.records), \
        "an over-budget, all-protected root pruned silently"


def test_a_sweep_that_actually_pruned_does_not_warn(tmp_path, caplog):
    """The other half, and it has to reach the same branch to mean anything.

    An UNDER-budget root returns before that branch exists, so asserting
    silence there proves nothing -- which a mutation showed: replacing the
    warning's guard with `True` left such a test green. This one goes over
    budget and leaves something prunable, so the function walks the same path
    and must still say nothing alarming.
    """
    import logging

    old = tmp_path / "old.log"
    old.write_bytes(b"x" * 1000)
    os.utime(old, (1, 1))                       # oldest, so it goes first
    keep = tmp_path / "keep.log"
    keep.write_bytes(b"x" * 10)

    with caplog.at_level(logging.WARNING, logger="runner.env"):
        freed, removed = prune_log_root(tmp_path, max_bytes=100)

    assert removed == [str(old)] and freed == 1000
    assert not caplog.records, \
        "a sweep that did its job warned as though it could not"


def test_the_files_tt_metal_holds_open_are_never_unlinked(tmp_path):
    """Unlinking a file a process still has open for write removes the NAME
    and frees nothing. tt-metal writes `generated/watcher/kernel_names.txt`
    and `kernel_elf_paths.txt` at device bring-up and holds both open for the
    life of every worker (measured with `lsof` on the live booth, Task 19),
    so an oldest-first sweep would eat them for no gain -- the same trap this
    project has hit three times.

    They are also the OLDEST files in the tree, written once at bring-up,
    which is exactly what an oldest-first sweep reaches for first. This
    fixture reproduces that.

    Mutation: dropping `_is_held_open_by_tt_metal` from the skip. Red.
    """
    watcher = tmp_path / "generated" / "watcher"
    watcher.mkdir(parents=True)
    held = []
    for name in ("kernel_names.txt", "kernel_elf_paths.txt"):
        path = watcher / name
        path.write_bytes(b"x" * 1000)
        os.utime(path, (1, 1))                  # oldest: swept first
        held.append(path)
    prunable = tmp_path / "recent.log"
    prunable.write_bytes(b"x" * 1000)
    os.utime(prunable, (10_000, 10_000))

    freed, removed = prune_log_root(tmp_path, max_bytes=1500)

    for path in held:
        assert path.exists(), f"{path.name} was unlinked; that frees nothing"
    assert removed == [str(prunable)], \
        "the sweep should have taken the one file it could actually free"
    assert freed == 1000


def test_an_unrelated_file_of_the_same_name_is_still_prunable(tmp_path):
    """Matched on the `watcher/` parent too, so the exemption is as narrow as
    the fact behind it. Without this, any file called kernel_names.txt
    anywhere in the tree would become undeletable.

    Mutation: matching on the name alone. Red.
    """
    other = tmp_path / "somewhere" / "kernel_names.txt"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x" * 1000)
    os.utime(other, (1, 1))
    (tmp_path / "b.log").write_bytes(b"x" * 10)

    _freed, removed = prune_log_root(tmp_path, max_bytes=100)

    assert removed == [str(other)], \
        "a file tt-metal is not holding open was treated as though it were"



# ---------------------------------------------------------------------------
# weights_cache() -- the one place this repo decides where the weights live.
#
# Before this existed, four callers derived it four ways and none of them
# honoured $TT_BIO_CACHE, which is the variable tt-bio itself prefers. They
# all happened to agree on ~/.boltz by default, so the disagreement was
# invisible until somebody set a variable -- at which point the doctor would
# check one directory, the postinst download into a second, and the folder
# load from a third.
# ---------------------------------------------------------------------------

def test_tt_bio_cache_wins_over_boltz_cache(tmp_path):
    """$TT_BIO_CACHE is the knob tt-bio documents as relocating everything, so
    it must outrank the older $BOLTZ_CACHE rather than the other way round. A
    resolver that checked BOLTZ_CACHE first would send a fold to the cache the
    operator moved *away* from."""
    from runner.env import weights_cache

    new, old = tmp_path / "new", tmp_path / "old"
    got = weights_cache({"TT_BIO_CACHE": str(new), "BOLTZ_CACHE": str(old)})
    assert got == new


def test_boltz_cache_is_still_honoured_when_tt_bio_cache_is_unset(tmp_path):
    """The older variable keeps working; booths already in the field set it."""
    from runner.env import weights_cache

    old = tmp_path / "old"
    assert weights_cache({"BOLTZ_CACHE": str(old)}) == old


def test_the_default_is_the_dot_boltz_directory_under_home():
    """The literal default, pinned as a literal. Every caller this replaces
    hardcoded `~/.boltz`, and a booth whose default silently moved would look
    fully healthy while folding against an empty cache."""
    from runner.env import weights_cache

    assert weights_cache({"HOME": "/home/somebody"}) == Path("/home/somebody/.boltz")


def test_an_empty_variable_is_ignored_rather_than_resolving_to_the_cwd(tmp_path):
    """`TT_BIO_CACHE=` in a systemd unit or a sourced env file is how a
    variable gets set to the empty string without anyone meaning to. Treating
    that as a path resolves to the process's working directory, which for the
    daemon is wherever run-demo.sh happened to start it."""
    from runner.env import weights_cache

    got = weights_cache({"TT_BIO_CACHE": "", "BOLTZ_CACHE": "", "HOME": "/home/somebody"})
    assert got == Path("/home/somebody/.boltz")


def test_a_tilde_in_the_variable_is_expanded(tmp_path):
    """An operator typing TT_BIO_CACHE=~/big-disk/boltz into a unit file gets
    the literal string, tilde and all -- the shell never saw it to expand."""
    from runner.env import weights_cache

    got = weights_cache({"TT_BIO_CACHE": "~/big-disk/boltz", "HOME": "/home/somebody"})
    assert got == Path("/home/somebody/big-disk/boltz")


def test_it_resolves_exactly_where_the_pinned_tt_bio_resolves(tmp_path):
    """The contract that actually matters: our resolver and tt-bio's own
    `weights.cache_root()` must name the same directory, or the doctor checks
    one place and a fold loads from another. Checked against the PINNED
    tt-bio, so an upstream change to the precedence breaks this test rather
    than a booth."""
    from tt_bio import weights as tt_weights

    from runner.env import weights_cache

    for env in (
        {"TT_BIO_CACHE": str(tmp_path / "a"), "BOLTZ_CACHE": str(tmp_path / "b")},
        {"BOLTZ_CACHE": str(tmp_path / "b")},
        {},
    ):
        saved = {k: os.environ.get(k) for k in ("TT_BIO_CACHE", "BOLTZ_CACHE")}
        try:
            for k in saved:
                os.environ.pop(k, None)
            os.environ.update(env)
            assert weights_cache(dict(os.environ)) == tt_weights.cache_root(), (
                f"our resolver and tt_bio.weights.cache_root() disagree for {env}")
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


def test_the_shell_resolver_answers_exactly_what_the_python_one_does(tmp_path):
    """scripts/weights-cache.sh and runner.env.weights_cache() are the same
    rule written twice, once per language, because a postinst and a doctor
    cannot import python and a fold cannot source bash.

    Twice is the minimum, and it is still twice -- so it is checked
    BEHAVIOURALLY, across the matrix that actually distinguishes the
    implementations, rather than by hoping two files stay in step. Each row
    below is a case that a plausible mis-edit gets wrong: precedence, empty
    values, and the bare default.
    """
    import subprocess
    from pathlib import Path as P

    from runner.env import weights_cache

    repo = P(__file__).resolve().parents[3]
    resolver = repo / "scripts" / "weights-cache.sh"
    assert resolver.is_file(), f"{resolver} is missing; this test is vacuous"

    matrix = [
        {"TT_BIO_CACHE": str(tmp_path / "a"), "BOLTZ_CACHE": str(tmp_path / "b"),
         "HOME": str(tmp_path)},
        {"BOLTZ_CACHE": str(tmp_path / "b"), "HOME": str(tmp_path)},
        {"HOME": str(tmp_path)},
        {"TT_BIO_CACHE": "", "BOLTZ_CACHE": "", "HOME": str(tmp_path)},
        {"TT_BIO_CACHE": "", "BOLTZ_CACHE": str(tmp_path / "b"), "HOME": str(tmp_path)},
    ]
    for env in matrix:
        r = subprocess.run(
            ["bash", "-c", f'. "{resolver}"; tt_bio_demo_weights_cache'],
            capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        shell = r.stdout.strip()
        python = str(weights_cache(env))
        assert shell == python, (
            f"for {env}:\n  shell  says {shell}\n  python says {python}")
