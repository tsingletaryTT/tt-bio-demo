# Vendored: tensix-viz

`tensix-viz.js` and `tensix-viz.css` in this directory are **not this project's
code**. They are copied verbatim from:

- **Project:** tensix-viz — "Tenstorrent hardware topology visualizer, chip to cluster"
- **Upstream:** https://github.com/tsingletaryTT/tensix-viz
- **Version:** 1.2.0
- **Commit:** `3986ed580388d040af3afcd73b2b34fb8279ea9d` (`chore(release): bump version to 1.2.0`)
- **Licence:** Apache-2.0 — the same licence this repository ships under
  (see `../../../LICENSE`), so no additional licence text is required here;
  this file is the attribution.
- **Copied on:** 2026-08-12

## Why vendored rather than fetched

The booth runs **offline at the venue** (see the README's Requirements: "Network
at provisioning time; **none required at the venue**"). The upstream project
offers a CDN embed; a CDN embed is a demo that goes blank the moment the
conference wifi does. These two files are self-contained — zero runtime
dependencies, no network access of their own — so a byte-for-byte copy on disk
is both the smallest and the most reliable way to ship them.

They are loaded by `ui/chipviz.py`, which reads them off disk and inlines them
into a single `about:blank` page. Nothing here is ever fetched over a network at
runtime.

## How this page is contained (and why the WebKit sandbox is off)

`ui/chipviz.py` sets `WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1` before
importing WebKit. That is not a development shortcut — it is load-bearing for
the booth, and this is the record of the decision:

- **Why it is necessary.** WebKitGTK runs its web process inside a bubblewrap
  sandbox, which needs an unprivileged user namespace. Ubuntu 24.04 restricts
  exactly that by default (`kernel.apparmor_restrict_unprivileged_userns = 1`,
  confirmed on this box), so `bwrap` fails and WebKit answers with a `g_error`.
- **Why it cannot be handled instead.** A `g_error` is not a Python exception.
  It is SIGTRAP; it kills the whole process, and no `try/except` anywhere can
  catch it. Every other failure mode in that module degrades to a hidden panel.
  This one would abort the kiosk at startup, in front of visitors, with nothing
  on screen. Fail-soft is not reachable by guarding here — only by not
  provoking it.
- **Why the blast radius is acceptable.** The sandbox exists to contain hostile
  web content, and this WebView renders none: the two files in this directory,
  inlined into one `about:blank` page, with no navigation, no network, and no
  remote or user-supplied bytes anywhere in the process. The generated page also
  declares `Content-Security-Policy: default-src 'none'` (with `'unsafe-inline'`
  for exactly the inline script and style it is made of), so those properties
  are enforced by the engine rather than assumed by us.
- **How to turn it back on.** The variable is set with `setdefault`, so an
  operator on a machine where the sandbox does work can export
  `WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=0` and keep it.

## Updating

Copy the two files again from an upstream checkout and update the version and
commit above:

    cp ~/code/tensix-viz/tensix-viz.{js,css} ui/assets/tensix-viz/

`tests/unit/test_chipviz.py` asserts both files are present and non-trivial, so
a half-finished update fails the suite rather than silently shipping a blank
animation. Do **not** hand-edit these files: local edits would be lost on the
next copy, and `ui/chipviz.py` deliberately keeps every project-specific
decision (grid layout, per-chip fan-out, telemetry mapping) on the Python side
for exactly that reason.
