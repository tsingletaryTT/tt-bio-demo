#!/usr/bin/env bash
#
# publish-release.sh -- attach this tag's packages to its GitHub Release,
# creating the release if it is not there yet.
#
#   publish-release.sh TAG NOTES_FILE FILE [FILE...]
#
# Called by .github/workflows/packages.yml's release job. It lives here rather
# than inline in the YAML because logic inside a workflow step can only ever
# be observed by tagging a release -- which is exactly the thing that went
# wrong, and the most expensive possible place to find out. See
# tests/unit/test_publish_release.py.
#
# THE INVARIANT, unchanged:
#
#     A published .deb is never replaced. Someone may already have installed
#     it, and package managers are built on the assumption that a fixed
#     version number means fixed contents.
#
# WHAT CHANGED, and why. That invariant is a statement about ASSETS. It used
# to be implemented as a statement about the release EXISTING:
#
#     if gh release view "$TAG"; then exit 1; fi
#
# Those two come apart in one case, and it is a case a person hits by taking
# the obvious path. Creating a release in the GitHub UI makes the tag AND the
# release atomically; the tag push then starts this workflow, which finds a
# release already there and refuses -- before uploading anything. v0.5.5 was
# published that way with ZERO assets, while INSTALL.md tells operators to
# `gh release download --pattern '*.deb'`. Nothing looked wrong: the run was
# red, but the release page was fine and empty.
#
# So the check now asks the question the invariant actually asks -- does this
# release have assets somebody could have installed? -- and an empty release
# is filled instead of refused.
set -euo pipefail

if [ "$#" -lt 3 ]; then
    echo "usage: publish-release.sh TAG NOTES_FILE FILE [FILE...]" >&2
    exit 2
fi

TAG="$1"; shift
NOTES="$1"; shift

[ -s "$NOTES" ] || { echo "::error::$NOTES is empty; refusing to publish a release with no description" >&2; exit 1; }

if ! gh release view "$TAG" >/dev/null 2>&1; then
    echo "creating release $TAG"
    gh release create "$TAG" \
        --title "tt-bio-demo ${TAG}" \
        --notes-file "$NOTES" \
        "$@"
    exit 0
fi

# It exists. The only question that matters is whether anything is IN it.
asset_count="$(gh release view "$TAG" --json assets --jq '.assets | length' 2>/dev/null || echo unknown)"

case "$asset_count" in
    0)
        : # empty -- fall through and fill it
        ;;
    unknown)
        # Could not read the asset list. Refuse: the safe answer when we
        # cannot tell whether a published .deb is at risk is the one that
        # does not overwrite it.
        echo "::error::release ${TAG} exists but its assets could not be read; refusing to touch it." >&2
        exit 1
        ;;
    *)
        echo "::error::release ${TAG} already has ${asset_count} asset(s); refusing to replace them. Bump the version and tag again." >&2
        exit 1
        ;;
esac

echo "release ${TAG} exists but is empty -- filling it"
gh release upload "$TAG" "$@" --clobber

# Only if nobody wrote a description. A release created by hand usually has
# one, and the changelog notes are a default for an empty body, not a
# correction of someone's prose.
body="$(gh release view "$TAG" --json body --jq '.body' 2>/dev/null || echo "")"
if [ -z "${body//[[:space:]]/}" ]; then
    echo "its body is empty -- setting the notes from debian/changelog"
    gh release edit "$TAG" --notes-file "$NOTES"
else
    echo "leaving the existing release description alone"
fi
