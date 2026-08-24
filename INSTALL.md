# Installing tt-bio-demo on a booth machine

This document starts from **a QB2 that has just had the most recent `tt-installer` run on
it** and ends at a booth folding proteins in front of visitors.

If your starting point is a development checkout rather than a booth machine, you want
[`README.md`](README.md#quick-start) instead — `scripts/setup-venvs.sh --dev` and
`scripts/run-demo.sh` from the repo, no packages involved.

> **Do not run this on a shared development box.** These packages install into `/opt`,
> register a `systemd --user` service, and (if you let them) install kernel modules.
> `dpkg -i` on a shared host is forbidden by this project's own packaging plan — install
> tests belong in a throwaway container (`scripts/deb-container.sh`). This document
> describes a machine that is going to a conference.

---

## 0. What `tt-installer` already gave you

Everything in this column is assumed present and is **not** re-installed by anything below:

| Provided by `tt-installer` | Why it matters here |
|---|---|
| `tt-kmd` kernel module, loaded and bound | The container/venv can only *consume* `/dev/tenstorrent/*`; nothing in this repo can create it |
| `/dev/tenstorrent/0..3` present | Four chips on two p300c boards — see [Hardware](#hardware-this-expects) |
| Hugepages configured | tt-metal will not open a device without them |
| Firmware flashed, `tt-smi` on `PATH` | The booth's chip telemetry panel shells out to `tt-smi -s` every two seconds |

Confirm all four in one command before going further:

```bash
tt-smi -s --snapshot_no_tty | head -40
```

You want four chip entries sharing two `board_id` values. If `tt-smi` reports nothing, stop
here — this is a `tt-installer` problem, not a tt-bio-demo problem, and nothing below will
work around it.

### What `tt-installer` did *not* give you

- The GTK4 / OpenGL system libraries the UI needs (`python3-gi`, `gir1.2-gtk-4.0`, `libgl1`, …)
- tt-bio itself, or the torch/ttnn stack it sits on
- The SFPI RISC-V cross-toolchain tt-bio's kernels compile against
- The model weights (3.7 GB)
- The application

Steps 1–4 supply exactly those, in that order.

---

## 1. Install the packages

**Download the packages from the latest release, then install them.** Nothing is built and
nothing is added to your apt sources:

```bash
gh release download --repo tsingletaryTT/tt-bio-demo --pattern '*.deb'
sudo apt install ./*.deb
```

Without `gh` — and a freshly imaged QB2 will not have it — plain `curl` does the same. This
is the exact sequence, verified end to end against a clean Ubuntu 24.04 with nothing but
`curl` installed:

```bash
mkdir -p ~/tt-bio-demo-pkgs && cd ~/tt-bio-demo-pkgs
BASE=https://github.com/tsingletaryTT/tt-bio-demo/releases/download/v0.4.0
for f in tt-bio-demo_0.4.0_all.deb \
         tt-bio-demo-runtime_0.4.0_amd64.deb \
         tt-bio-demo-weights_0.4.0_all.deb \
         tt-bio-demo-all_0.4.0_all.deb; do
    curl -fsSLO "$BASE/$f"
done
sudo apt install ./*.deb
```

The repository is public, so neither path needs a token or a login. Substitute a newer tag
and version if a later release exists — see
[the releases page](https://github.com/tsingletaryTT/tt-bio-demo/releases).

`apt install ./…` rather than `dpkg -i` is deliberate: it resolves the apt dependencies in
`debian/control` instead of failing on them.

### Building them yourself instead

Only needed to install a commit that has not been released. `dist/` is gitignored, so a clone
contains no packages:

```bash
./scripts/build-deb.sh                 # writes the four .debs into dist/
sudo apt install ./dist/*.deb
```

Build on a dev box or in the container harness and copy the `.deb`s across. Building on the
booth machine means putting a build toolchain on a machine going to a conference, which is
the opposite of what this packaging is for.

**Two debconf prompts appear, and the safe answer to both is the default (No):**

| Prompt | Default | What to answer on a post-`tt-installer` box |
|---|---|---|
| `Run "tt-bio install-deps" now?` | No | **No.** `tt-installer` has already installed the Tenstorrent system packages and kernel modules this would fetch. Saying yes re-runs a kernel-module installer you do not need. |
| `Download the model weights now (3.7 GB)?` | No | **No here, yes in step 3.** The download is better run deliberately, where you can watch it, than under the dpkg lock. |

The install finishes by printing `ONE STEP LEFT` and the exact command for step 2. That is
expected — the postinst deliberately does not build the Python environments while apt holds
the dpkg lock.

### What landed where

```
/opt/tt-bio-demo/              the application, playlist, scripts
/opt/tt-bio-demo/.venvs/       (empty until step 2)
~/.config/systemd/user/        tt-bio-demo.user.service  (see step 5)
/usr/share/applications/       com.tenstorrent.ttbio.demo.desktop
/usr/share/icons/hicolor/      the app icon, 16px through 512px
```

The icon and the desktop entry make the booth **discoverable without a terminal**: after
install it appears in the application launcher as *tt-bio Protein Folding Demo*, filed under
**Science** (shown as *Science & Math* or *Education & Science* depending on the desktop),
with the Tenstorrent mark as its icon.

If the icon does not appear immediately, the desktop's caches are stale rather than the
install being wrong:

```bash
sudo update-desktop-database
sudo gtk-update-icon-cache -f /usr/share/icons/hicolor
```

---

## 2. Build the two Python environments

```bash
sudo /opt/tt-bio-demo/scripts/setup-venvs.sh --prefix /opt/tt-bio-demo
```

This is the long step: it downloads gigabytes and takes minutes. It builds two venvs that
are deliberately kept apart, because a torch/ttnn stack and PyGObject have no business
sharing a `site-packages`:

| venv | Built how | Holds |
|---|---|---|
| `venv-ui` | system `python3` **with** `--system-site-packages` | PyGObject (GTK4), gemmi, PyOpenGL, numpy |
| `venv-runner` | isolated, no system packages | torch, ttnn, tt-bio **0.7.0** (pinned release), vendored SFPI |

Note `--dev` is **not** passed. That flag adds pytest to `venv-runner`, and test tooling is
dead weight and extra supply-chain surface on a booth machine.

**Read the exit code — it is three-valued and the middle one is easy to miss:**

| Exit | Meaning | Do what |
|---|---|---|
| `0` | Everything built and verified, including a real device open/close | Continue |
| `1` | Hard failure — bad preconditions, missing apt packages, a failed pip install or SFPI hash check | Fix and re-run |
| `2` | **Built but non-functional** — venv exists with the right pin, but its torch/ttnn/tt_bio stack will not import, or hardware is present and unusable (driver unbound, failed `open_device()`, wedged card) | Do **not** ship the box. Investigate before the venue. |

A second run without `--force` is a ~0.3–0.5 s no-op, so re-running to confirm is cheap.

---

## 3. Fetch the model weights

**The venue is offline. This must happen before the machine leaves.**

```bash
sudo dpkg-reconfigure tt-bio-demo-weights      # answer Yes this time
```

That pulls two artifacts totalling ~3.7 GB through tt-bio's own Hugging Face client:

- `protenix-v2.pt` — 1.86 GB
- `mols.tar` — the CCD molecule library, 1.85 GB

The download is **resumable**: an interrupted attempt continues rather than restarting. The
postinst verifies what landed and prints `weights present and verified. The booth can fold
offline.` — treat any other final line as a failure.

---

## 4. Verify the machine

```bash
/opt/tt-bio-demo/scripts/doctor.sh          # check everything, change nothing
/opt/tt-bio-demo/scripts/doctor.sh --fix    # also perform the safe repairs
```

Then prove it end to end with the cheapest real fold — Trp-cage, 20 residues, ~4.4 s warm:

```bash
/opt/tt-bio-demo/scripts/run-demo.sh --targets trpcage
```

A structure condensing out of a point cloud on real silicon is the only acceptance test
that counts. `Ctrl-C` in the terminal tears the daemon down cleanly.

> **Budget real time for the first fold — about a minute and a half per target.** Every fold
> time quoted in this project is measured **warm**. Measured 2026-08-17 on an empty kernel
> cache: Trp-cage's first fold takes **94.5 s** (of which ~83 s is kernel compilation) against
> **9.4 s** warm. Full numbers and method in [`docs/cold-start.md`](docs/cold-start.md).
>
> That is per *target*, and the six playlist targets share kernels unevenly, so the cost of
> warming the whole playlist is not simply six times it — and has not been measured. Do not do
> your first fold in front of visitors.
>
> The `tt-bio-demo-weights` package description claims it "pre-warms the tt-metal kernel
> cache." **It does not** — the postinst contains no pre-warm code, only weight download and
> verification. Until that is implemented, warming the cache means running step 4's fold
> once per target you intend to show.

---

## 5. Launch at the booth

Two supported ways, and they are not interchangeable:

### Desktop entry (what an operator uses)

Double-click **tt-bio Protein Folding Demo**, or run:

```bash
/opt/tt-bio-demo/scripts/run-demo.sh          # >1 chip starts in the 2×2 grid; --solo for one
```

`run-demo.sh` starts *both* halves — the daemon in `venv-runner`, the UI in `venv-ui` — and
tears the daemon down on exit. This is the normal path.

### Supervised daemon (what runs all day)

For an unattended booth, let systemd own the daemon so it comes back by itself:

```bash
systemctl --user enable --now tt-bio-demo
systemctl --user status tt-bio-demo
```

It is a **`--user`** service, not a system one: the daemon serves its socket out of the
user's runtime directory, where the UI — an ordinary desktop application — can reach it
without permission plumbing. `Restart=on-failure` (not `always`) means a deliberate
`systemctl --user stop` stays stopped.

If the booth must survive a reboot with nobody logged in, enable lingering:

```bash
sudo loginctl enable-linger "$USER"
```

---

## Hardware this expects

- **Tenstorrent QB2** — 2× p300c Blackhole *boards* presenting **4 chips**. `tt-smi` lists
  one entry per chip; the two chips of a p300c share a `board_id`. A visitor reading
  "4 cards" would picture the wrong machine, and the booth's own panel says so.
- **Ubuntu 24.04, Wayland**
- **Network at provisioning time; none required at the venue**

A single fold is a **single-chip** fold — that is tt-bio's documented limit, not something
this demo works around. Four chips buy four proteins at once and a shorter wait for a
visitor's pick; they do not make any one fold faster.

---

## Troubleshooting the install

**`ModuleNotFoundError: gi` or `gemmi`** — something ran a bare `python3`. Every entry point
must go through `venv-ui` or `venv-runner`; never a system or personal interpreter.

**`setup-venvs.sh` exits 2** — see the table in step 2. The venv is built but its stack does
not import, or a chip is present and unusable. This is the failure mode that silently ships
a dead booth.

**The booth will not stop when signalled by pid** — `kill -INT <pid>` on `run-demo.sh` does
nothing visible: the launcher's trap cannot run until its foreground command returns. Signal
the whole process *group*, which is what a terminal `Ctrl-C` does:

```bash
kill -INT -$(ps -o pgid= -p <pid> | tr -d ' ')     # note the '-' before the pgid
```

One INFO line about a `tt-smi` sample killed by a signal is expected on a clean stop.

**A chip is missing from telemetry** — check `tt-smi -s` directly. A chip the daemon has
quarantined for temperature is deliberately withheld from scheduling. **Never run
`tt-smi -r` on a shared machine.**

**tt-metal filling the disk** — the daemon's unit passes `--log-root` into the user runtime
directory for a measured reason: tt-metal writes Inspector/Watcher output relative to the
working directory, and this project measured 13–14 MB/s going into an already-unlinked file,
invisible to a directory walk and enough to exhaust a tmpfs in ~31 minutes. Do not launch
the daemon without `--log-root`.

---

## What is verified, and what is not

Written against release **v0.4.0**.

**Step 1 is verified end to end.** The exact `curl` sequence above was run against a clean
`ubuntu:24.04` container with nothing preinstalled: the four assets downloaded from the
release, `apt install ./*.deb` succeeded, and all four packages reached
`install ok installed`. CI re-proves the equivalent on every push, and a release cannot be
published unless it passes.

**Steps 2–5 are not.** The package contents, debconf templates, postinst behaviour, unit file
and script flags were read from source and are individually tested, but **no full
clean-machine install has been run against a freshly imaged QB2** — CI has no Tenstorrent
hardware and never will. Expect step 2 (the venv build, which needs network and the SFPI
toolchain) and step 3 (3.7 GB of weights) to be where a real first run finds something.

**Budget the first fold.** Step 4's fold on a machine that has never folded that target costs
~94.5 s rather than the warm ~9 s — see [`docs/cold-start.md`](docs/cold-start.md). That is
measured, not estimated.
