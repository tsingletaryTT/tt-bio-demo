# Vendored: tensix-viz

`tensix-viz.js` and `tensix-viz.css` in this directory are **not this project's
code**. They are copied verbatim from:

- **Project:** tensix-viz — "Tenstorrent hardware topology visualizer, chip to cluster"
- **Upstream:** https://github.com/tsingletaryTT/tensix-viz
- **Version:** 1.3.0
- **Commit:** `ddf626705641ab09815fd9cb6a1fd6267af740a5` (`chore(release): bump version to 1.3.0`)
- **Licence:** Apache-2.0 — the same licence this repository ships under
  (see `../../../LICENSE`), so no additional licence text is required here;
  this file is the attribution.
- **Copied on:** 2026-09-24

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

    scripts/update-tensix-viz.sh [path-to-tensix-viz-checkout]

Copies both files from the upstream checkout (default `~/code/tensix-viz`)
and rewrites this file's own Version/Commit/Copied-on lines to match, in one
step -- previously two manual edits and a `cp`, the exact shape of drift this
project has been burned by before (a manual `cp` nobody had written down for
the thumbnails, a shell/Python cache-path pair that quietly disagreed). It
refuses an upstream checkout that is not git, or is git but has uncommitted
changes, so this file never attributes a vendored copy to a commit that
does not actually exist. `tests/unit/test_update_tensix_viz_sh.py` runs the
real script against a synthetic upstream repo.

Look at the panel afterwards (`T` in the running booth, or
`scripts/refresh-screenshots.sh`) and run `scripts/test.sh` before
committing -- a half-finished update should fail the suite rather than
silently ship a blank animation. Do **not** hand-edit these files: local
edits would be lost on the next copy, and `ui/chipviz.py` deliberately keeps
every project-specific decision (grid layout, per-chip fan-out, telemetry
mapping) on the Python side for exactly that reason.
