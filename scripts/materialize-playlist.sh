#!/usr/bin/env bash
# tt_bio_demo_materialize_playlist -- the one place a manifest becomes a
# directory of fold-input YAMLs the daemon can glob.
#
# WHY THIS EXISTS AS ITS OWN FILE, not inline in run-demo.sh any more.
# `runner/daemon.py`'s `_playlist_files()` enumerates `--playlist`'s
# directory by globbing `*.yaml` (excluding `manifest.yaml`/`questions.yaml`
# by name -- see that module's own `_NON_FOLD_PLAYLIST_FILENAMES` comment).
# `scripts/run-demo.sh` fed that glob correctly, by symlinking each selected
# target's real input file into a RUNTIME directory it owns, one symlink
# per target named by manifest id. The packaged/systemd deployment did not:
# `debian/tt-bio-demo.install` ships `playlist/manifest.yaml` and
# `playlist/questions.yaml` into `/opt/tt-bio-demo/playlist/` and the real
# fold-input YAMLs into the SIBLING `/opt/tt-bio-demo/examples/` --
# `manifest.yaml`'s own `input:` entries point `../examples/...` into that
# sibling on purpose -- so `--playlist /opt/tt-bio-demo/playlist` (what
# `scripts/tt-bio-demo-daemon-launcher.sh` passed) glob-matched ONLY the two
# metadata files, which the `_NON_FOLD_PLAYLIST_FILENAMES` exclusion then
# correctly threw out, leaving NOTHING: a packaged/systemd booth enumerated
# zero fold targets and idled forever. (PR review, Copilot.)
#
# The fix is to give the packaged path the same farm the dev path already
# had, rather than inventing a second way to resolve a target -- one
# function, called from both `run-demo.sh` (a per-run temp/runtime dir it
# already owned) and `tt-bio-demo-daemon-launcher.sh` (a NEW runtime dir
# under `%t/tt-bio-demo/playlist`, not the installed `/opt/tt-bio-demo/
# playlist/` -- that directory is root-owned at install time and an
# unprivileged `systemd --user` service has no business writing symlinks
# into it even if permissions happened to allow it).
#
# `ui/playlist.py` is the parser for both processes (no `gi`/GTK import at
# all -- plain `yaml` + stdlib), so a manifest either process would refuse
# is refused here too, with that module's own one-line message, never a
# traceback three layers of subprocess away from where the answer lives.

# tt_bio_demo_materialize_playlist <python> <manifest> <targets-csv-or-empty> <dest-dir>
#
# Symlinks every SELECTED target's real fold-input YAML into <dest-dir> as
# <target_id>.yaml (re-pointed every call, `ln -sf`, so a stale link from an
# older checkout or an earlier selection never lingers) and removes every
# symlink <dest-dir> owns that no longer belongs. Only symlinks are ever
# touched or deleted -- <dest-dir> is expected to be a directory this
# function's caller owns outright, never the installed `playlist/` tree
# itself.
#
# Returns non-zero (with a message on stderr, no traceback) on: a manifest
# `<python> -m ui.playlist` itself refuses, a selected target naming an
# input file that does not exist, or a selection that resolves to zero
# targets -- the same three refusals `run-demo.sh` already made before this
# was factored out, unchanged.
tt_bio_demo_materialize_playlist() {
    local python_bin="$1" manifest="$2" targets_csv="$3" dest_dir="$4"
    local lines
    if ! lines="$("$python_bin" -m ui.playlist "$manifest" ${targets_csv//,/ } 2>&1)"; then
        printf 'materialize-playlist: %s\n' "$lines" >&2
        printf 'materialize-playlist: --playlist must name a playlist MANIFEST,\n' >&2
        printf 'and every selected target id must appear in it.\n' >&2
        return 1
    fi

    find "$dest_dir" -maxdepth 1 -type l -name '*.yaml' -delete

    local count=0
    local target_id input_path
    while IFS=$'\t' read -r target_id input_path; do
        [ -n "$target_id" ] || continue
        if [ ! -f "$input_path" ]; then
            printf 'materialize-playlist: target %s names an input that does not exist:\n' \
                "$target_id" >&2
            printf '  %s\n' "$input_path" >&2
            printf 'The gallery must never offer something the daemon cannot fold.\n' >&2
            return 1
        fi
        ln -sf "$input_path" "${dest_dir}/${target_id}.yaml"
        count=$((count + 1))
    done <<< "$lines"

    if [ "$count" -eq 0 ]; then
        printf 'materialize-playlist: %s selected no targets; there would be\n' "$manifest" >&2
        printf 'nothing to fold.\n' >&2
        return 1
    fi
    return 0
}
