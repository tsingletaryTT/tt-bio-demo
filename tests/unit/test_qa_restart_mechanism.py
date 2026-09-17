"""The plumbing that connects Ctrl+A (`ui/app.py`) to a real restart
(`scripts/run-demo.sh`), rather than the behaviour on either end of it.

Two review findings, both about connective tissue that had ZERO test
coverage even though the feature around it was thoroughly tested:

1. **`main()`'s exit-code bridge.** `DemoApp._request_qa_restart` sets
   `self.exit_code` and calls `self.quit()`, which stops
   `Gio.Application.run()` -- but `run()` always returns 0 on a clean quit,
   with no notion of a custom process exit code. `main()`'s last line reads
   `app.exit_code` back out and returns THAT instead, which is the only
   thing that makes `scripts/run-demo.sh`'s exit-code check downstream see
   anything but 0. Nothing in the rest of the suite calls `ui.app.main()` at
   all (every other test drives `DemoApp` directly), so that one line could
   be silently changed to `return result` -- disabling Ctrl+A entirely,
   turning it into "quit the booth" -- and the whole existing suite would
   stay green.
2. **The sentinel exit code and the env-var name are independently
   duplicated literals.** `42` and `"TT_BIO_DEMO_RUN_DEMO_SH"` each appear
   once in `ui/app.py` (as Python constants) and once in
   `scripts/run-demo.sh` (as shell variables), with nothing checking they
   still agree -- the exact shape
   `tests/unit/test_weights_cache_is_derived_once.py` already guards against
   for the weights-cache variables, applied here to this feature's own pair
   of duplicated literals.
"""

import re
from pathlib import Path

import pytest

from ui import app as app_module

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUN_DEMO_SH = REPO_ROOT / "scripts" / "run-demo.sh"


# ---------------------------------------------------------------------------
# 1. main()'s exit-code bridge
# ---------------------------------------------------------------------------

class _StubDemoApp:
    """Enough of `DemoApp` for `main()` to drive: a constructor that accepts
    every keyword `main()` passes, and a `run()` that returns a plain int
    the way `Gio.Application.run()` does. No GTK, no socket, no device --
    `main()` itself never touches any of those directly; it only calls
    through to whatever class `ui.app.DemoApp` names, which is exactly what
    this stub replaces.
    """

    last_kwargs = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        self.exit_code = None
        self._run_returns = 0

    def run(self, argv):
        assert argv == []
        return self._run_returns


def test_main_returns_the_sentinel_not_runs_plain_return_value(monkeypatch):
    """The bridge itself: when `_request_qa_restart` has stashed the
    sentinel on `app.exit_code`, `main()` must return THAT, not whatever
    `run()` handed back (always 0 for a clean `Gio.Application.quit()`).

    Mutation this guards against: `main()`'s last line silently becoming
    `return result` -- which would make Ctrl+A functionally equivalent to
    quitting the booth, since scripts/run-demo.sh would then see exit code 0
    and never restart with --questions. Nothing else in this suite calls
    `ui.app.main()`, so nothing else could have caught this.
    """
    sentinel = app_module.QUESTIONS_RESTART_EXIT_CODE

    class _RestartingStub(_StubDemoApp):
        def run(self, argv):
            self.exit_code = sentinel
            return 0  # what Gio.Application.run() actually returns

    monkeypatch.setattr(app_module, "DemoApp", _RestartingStub)

    assert app_module.main([]) == sentinel


def test_main_returns_runs_value_when_no_sentinel_was_set(monkeypatch):
    """The other half of the same line: an ordinary clean exit (no Ctrl+A,
    `exit_code` left at `None`) must still return whatever `run()` gave
    back, not always the sentinel or always 0. Pins the `if app.exit_code is
    not None else result` branch, not only the branch above it."""

    class _PlainStub(_StubDemoApp):
        def run(self, argv):
            return 7

    monkeypatch.setattr(app_module, "DemoApp", _PlainStub)

    assert app_module.main([]) == 7


def test_main_passes_parsed_arguments_to_demoapp(monkeypatch):
    """A sanity check that the stub above is actually exercising `main()`'s
    real argument-parsing path, not a bypassed no-op."""
    monkeypatch.setattr(app_module, "DemoApp", _StubDemoApp)

    app_module.main(["--socket", "/tmp/x.sock", "--targets", "trpcage,dhfr",
                      "--windowed", "--quad"])

    kwargs = _StubDemoApp.last_kwargs
    assert kwargs["socket_path"] == "/tmp/x.sock"
    assert kwargs["target_ids"] == ["trpcage", "dhfr"]
    assert kwargs["windowed"] is True
    assert kwargs["quad"] is True


def test_a_keyboard_interrupt_during_construction_returns_130_cleanly(monkeypatch):
    """Minor finding from the same review: `DemoApp(...)` construction was
    moved outside `main()`'s `try` block at some point, which would have let
    a Ctrl-C landing during GTK/GObject construction propagate out of
    `main()` as a bare KeyboardInterrupt traceback -- exactly the "a clean
    stop must not end in a traceback" rule this project holds everywhere
    else. Construction must be INSIDE the try, and the except must return
    130 without touching `app` (which may not exist yet)."""

    class _RaisingStub(_StubDemoApp):
        def __init__(self, **kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr(app_module, "DemoApp", _RaisingStub)

    assert app_module.main([]) == 130


# ---------------------------------------------------------------------------
# 2. The sentinel exit code and env-var name agree across the language
#    boundary (ui/app.py <-> scripts/run-demo.sh), the same discipline
#    test_weights_cache_is_derived_once.py already applies to the
#    weights-cache variables.
# ---------------------------------------------------------------------------

def _run_demo_sh_literal(pattern):
    text = RUN_DEMO_SH.read_text()
    match = re.search(pattern, text, re.MULTILINE)
    assert match, (
        f"pattern {pattern!r} not found in {RUN_DEMO_SH} -- has the shell "
        "side of this mechanism been renamed?")
    return match.group(1)


def test_the_exit_code_sentinel_agrees_with_run_demo_sh():
    shell_value = int(_run_demo_sh_literal(r'^QUESTIONS_RESTART_EXIT_CODE=(\d+)'))
    assert shell_value == app_module.QUESTIONS_RESTART_EXIT_CODE, (
        "scripts/run-demo.sh's QUESTIONS_RESTART_EXIT_CODE "
        f"({shell_value}) no longer matches ui.app.QUESTIONS_RESTART_"
        f"EXIT_CODE ({app_module.QUESTIONS_RESTART_EXIT_CODE}) -- a "
        "one-sided rename here silently breaks the whole restart "
        "mechanism: the UI would exit with a code the shell no longer "
        "recognises as the restart sentinel.")


def test_the_env_var_name_agrees_with_run_demo_sh():
    shell_value = _run_demo_sh_literal(r'^RUN_DEMO_SH_ENV_VAR="([^"]+)"')
    assert shell_value == app_module.RUN_DEMO_SH_ENV_VAR, (
        "scripts/run-demo.sh's RUN_DEMO_SH_ENV_VAR "
        f"({shell_value!r}) no longer matches ui.app.RUN_DEMO_SH_ENV_VAR "
        f"({app_module.RUN_DEMO_SH_ENV_VAR!r}) -- the UI would no longer "
        "recognise being launched by run-demo.sh, and Ctrl+A would always "
        "decline with 'no restart mechanism here'.")
