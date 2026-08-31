"""Every weights command this project PRINTS must be one tt-bio can run.

A user got the booth working and reported: "The docs were not quite right - I
had to discover a model downloading command, the doctor.sh didn't know about".
Two things had gone wrong. The command was missing from the docs and from
doctor.sh entirely -- and the reason nobody noticed is that nothing checks
printed commands against the tt-bio they are printed for.

This project already learned that lesson once on the other side of the same
boundary. `test_the_weights_postinst_uses_the_tt_bio_api_that_actually_exists`
parses the PINNED tt-bio's own source to verify the postinst's API calls, and
it has caught a real break twice: a first draft calling `hf_artifact` with the
wrong arity, and the 0.7.0 upgrade removing `tt_bio.main.hf_artifact`
outright. This is the same idea applied to the commands we tell a HUMAN to
type, which until now nothing verified at all.

Lives under tests/unit/runner/ because it imports tt_bio (cheap -- tt_bio.
weights pulls in os/shutil/pathlib and no torch), and that directory is the
one scripts/test.sh runs under venv-runner.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]

# Everything that tells a person to run a weights command. Docs and scripts
# together, because a command going stale in a script is the same defect as
# one going stale in a README -- and the reported bug was one of each.
_SOURCES = (
    "README.md",
    "INSTALL.md",
    "scripts/doctor.sh",
    "scripts/setup-venvs.sh",
    "debian/tt-bio-demo-weights.postinst",
)

# `tt-bio weights ...` up to the end of the line or a shell/markdown boundary.
_INVOCATION = re.compile(r"tt-bio\s+weights\b([^\n`'\"|;&)]*)")


def _invocations():
    for rel in _SOURCES:
        path = REPO / rel
        if not path.is_file():
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for m in _INVOCATION.finditer(line):
                yield rel, n, m.group(1).split()


def test_at_least_one_source_documents_the_command():
    """Guard against this whole file passing vacuously. If the regex stops
    matching -- a rename, a reflow, a different quoting style -- every test
    below goes green by finding nothing to check, which is precisely the
    failure mode that let the original bug ship."""
    found = list(_invocations())
    assert found, ("no `tt-bio weights` invocation found in any of "
                   f"{_SOURCES}; either the docs regressed or this test's "
                   "regex no longer matches how they are written")


def test_every_documented_flag_is_a_real_flag():
    """A flag that does not exist fails in front of the operator, at the exact
    moment they have already decided to trust the docs."""
    from tt_bio import main as tt_main

    real = set()
    for param in tt_main.weights_cmd.params:
        real.update(param.opts)
        real.update(param.secondary_opts)

    bad = []
    for rel, n, args in _invocations():
        for arg in args:
            if not arg.startswith("-"):
                continue
            name = arg.split("=", 1)[0]
            if name not in real:
                bad.append(f"{rel}:{n}: {name}")
    assert not bad, (
        f"these flags do not exist on `tt-bio weights` (it has {sorted(real)}):\n  "
        + "\n  ".join(bad))


def test_every_documented_model_is_a_real_model():
    """The ARTIFACT KEY, which is the half a type checker cannot help with.

    `tt-bio weights --download protenix-v2` is a string in a markdown file. If
    that row is renamed upstream, nothing but this test stands between the
    rename and an operator typing a command that errors."""
    from tt_bio import weights as tt_weights

    bad = []
    for rel, n, args in _invocations():
        for arg in args:
            if arg.startswith("-"):
                continue
            if arg not in tt_weights.MODEL_ARTIFACTS:
                bad.append(f"{rel}:{n}: {arg}")
    assert not bad, (
        "these are not models tt-bio knows "
        f"({sorted(tt_weights.MODEL_ARTIFACTS)}):\n  " + "\n  ".join(bad))


@pytest.mark.parametrize("rel", ["README.md", "INSTALL.md"])
def test_both_install_paths_tell_you_how_to_get_the_weights(rel):
    """THE REPORTED BUG, as a test.

    Whichever document an operator opens -- the README for a source install,
    INSTALL.md for the .deb -- it must name the command. Before this, INSTALL.md
    covered only `dpkg-reconfigure` and the README covered nothing at all, so a
    source install ended at `run-demo.sh` with no weights and no instructions.
    """
    text = (REPO / rel).read_text()
    assert "tt-bio weights --download protenix-v2" in text, (
        f"{rel} does not tell a reader how to fetch the model weights")


def test_the_model_the_docs_name_is_the_model_the_booth_folds():
    """The docs, preflight and the playlist must agree on ONE model. A README
    telling someone to download boltz2 for a booth that folds protenix-v2 is
    a 4.2 GB detour that ends with the same empty cache."""
    from runner.preflight import MODEL

    for rel, n, args in _invocations():
        models = [a for a in args if not a.startswith("-")]
        for m in models:
            assert m == MODEL, (
                f"{rel}:{n} names {m}, but the booth folds {MODEL}")
