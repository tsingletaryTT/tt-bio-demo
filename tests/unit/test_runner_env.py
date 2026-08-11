import os

from runner.env import LOG_ROOT_VAR, runner_environ


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


import os
import time

from runner.env import log_root_size, prune_log_root


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
