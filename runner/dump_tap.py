"""Extract the live diffusion trajectory from Protenix.

`OpenDDE.fold` exposes a public `dump_fn`; `Protenix.fold` does not, even though
the `edm_sample` it calls internally already accepts one. So the only way to
observe per-step coordinates is to replace the module-level
`tt_bio.protenix.edm_sample` before calling `fold()`.

That is a coupling to a private surface, deliberately confined to this file. A
patch adding the public parameter is prepared in
`docs/upstream/protenix-dump-fn/`; when it lands upstream, this module is the
only thing that has to change.

The failure this module exists to prevent: if `edm_sample` moves, is renamed, or
gains a caller that passes `dump_fn` itself, the fold still succeeds and produces
a correct structure — it just stops emitting frames. The demo would look like it
was working while its headline feature was dead. `check_tap_supported()` turns
that into a loud, specific error at startup instead.
"""

import inspect
import logging

log = logging.getLogger(__name__)


class TapUnavailable(Exception):
    """tt-bio's internals no longer match what the trajectory tap expects."""


def _protenix():
    import tt_bio.protenix as protenix  # imported lazily: pulls in torch
    return protenix


def check_tap_supported():
    """Raise TapUnavailable with an actionable message if the tap cannot work."""
    protenix = _protenix()

    fn = getattr(protenix, "edm_sample", None)
    if fn is None or not callable(fn):
        raise TapUnavailable(
            "tt_bio.protenix.edm_sample is missing or not callable; the trajectory "
            "tap targets it directly. tt-bio's internals have changed — check "
            "whether Protenix.fold now takes a public dump_fn (see "
            "docs/upstream/protenix-dump-fn/) and switch to it."
        )

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError) as exc:
        raise TapUnavailable(f"cannot inspect tt_bio.protenix.edm_sample: {exc}") from exc

    if "dump_fn" not in params:
        raise TapUnavailable(
            "tt_bio.protenix.edm_sample no longer accepts dump_fn; the trajectory "
            "tap cannot observe denoising steps through it."
        )
    return None


def install_trajectory_tap(on_frame):
    """Route every denoising step to `on_frame(sample, step, coords)`.

    `coords` arrives as an (N, 3) float32 numpy array, already on the host.

    The wrapper *overrides* any dump_fn the caller passes rather than deferring
    to it. Deferring (e.g. kwargs.setdefault) would silently stop intercepting
    the moment tt-bio's own code starts passing the parameter — which is exactly
    what the prepared upstream patch does.
    """
    import numpy as np

    check_tap_supported()
    protenix = _protenix()
    original = protenix.edm_sample

    def tapped(*args, **kwargs):
        caller_dump_fn = kwargs.get("dump_fn")

        def relay(step, x):
            if caller_dump_fn is not None:
                try:
                    caller_dump_fn(step, x)
                except Exception:
                    log.exception("caller's dump_fn raised; continuing")
            try:
                coords = np.asarray(
                    x.detach().cpu().numpy() if hasattr(x, "detach") else x,
                    dtype=np.float32,
                ).reshape(-1, 3)
                on_frame(0, int(step), coords)
            except Exception:
                # A consumer bug must never abort a fold that is otherwise fine.
                log.exception("trajectory callback raised; continuing the fold")

        kwargs["dump_fn"] = relay
        return original(*args, **kwargs)

    protenix.edm_sample = tapped
    return (protenix, original)


def remove_trajectory_tap(handle):
    """Restore the original edm_sample. Safe to call more than once."""
    protenix, original = handle
    if getattr(protenix, "edm_sample", None) is not original:
        protenix.edm_sample = original
