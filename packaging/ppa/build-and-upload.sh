#!/usr/bin/env bash
# Build and upload the Ubuntu PPA source package for each target series.
#
# Usage:
#   packaging/ppa/build-and-upload.sh <version> [--upload]
#
# Example:
#   packaging/ppa/build-and-upload.sh 0.11.2          # build only, source packages left in ./ppa-build
#   packaging/ppa/build-and-upload.sh 0.11.2 --upload  # build and dput to the PPA
#
# Requires: devscripts, dpkg-dev, debhelper (build-essential + these on a Debian/Ubuntu host).
# Signs with the GPG key below; override by exporting GPG_KEYID before running.
# debsign otherwise tries to match the Maintainer field in debian/control, which
# only works if that email is a UID on your key.

set -euo pipefail

# Only currently-supported, non-EOL series: resolute (26.04 LTS, GNOME 50)
# and stonking (26.10, GNOME 51). oracular/plucky/questing were all
# already obsolete on Launchpad by the time this was written (July 2026) -
# non-LTS Ubuntu releases only get ~9 months of support, so this list needs
# revisiting every time a new series ships.
PPA="ppa:yannmasoch/nautilus-my-computer"
SERIES=(resolute:26.04 stonking:26.10)
GPG_KEYID="${GPG_KEYID:-48A4A06AF4B9B0ED031FCB75E0092153561F1DB8}"

VERSION="${1:?usage: $0 <version> [--upload]}"
UPLOAD="${2:-}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_DIR="$REPO_ROOT/ppa-build"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# devscripts' `debuild`/`dpkg-buildpackage` require the orig tarball to sit
# next to the extracted source tree as <pkg>_<version>.orig.tar.gz.
git -C "$REPO_ROOT" archive --format=tar.gz \
    --prefix="nautilus-my-computer-$VERSION/" \
    -o "$BUILD_DIR/nautilus-my-computer_$VERSION.orig.tar.gz" HEAD

tar -xzf "$BUILD_DIR/nautilus-my-computer_$VERSION.orig.tar.gz" -C "$BUILD_DIR"
SRC_DIR="$BUILD_DIR/nautilus-my-computer-$VERSION"

# Each series gets its own version, <deb-version>~ubuntu<release>.1 (e.g. 0.13.3-1~ubuntu26.04.1).
#
# Only the orig tarball is shared between series.
#
# Derived from debian/changelog rather than hardcoded, so a packaging-only
# revision bump (e.g. 0.11.2-2 for a debian/ fix with no upstream change)
# doesn't also require editing this script.
DEB_VERSION="$(dpkg-parsechangelog -l "$REPO_ROOT/packaging/ppa/debian/changelog" -S Version)"

for entry in "${SERIES[@]}"; do
    series="${entry%%:*}"
    release="${entry#*:}"
    echo "== packaging for $series ($release) =="

    series_version="${DEB_VERSION}~ubuntu${release}.1"
    rm -rf "$SRC_DIR/debian"
    cp -r "$REPO_ROOT/packaging/ppa/debian" "$SRC_DIR/debian"
    (
        cd "$SRC_DIR"
        sed -i "1s/(${DEB_VERSION}) [^;]*;/(${series_version}) ${series};/" debian/changelog
        debuild -k"$GPG_KEYID" -S -sa
    )

    changes="$BUILD_DIR/nautilus-my-computer_${series_version}_source.changes"
    mkdir -p "$BUILD_DIR/$series"
    grep -oP '^ [0-9a-f]{32,64} \d+ \S+ \S+ \K\S+' "$changes" | while read -r f; do
        cp "$BUILD_DIR/$f" "$BUILD_DIR/$series/"
    done
    cp "$changes" "$BUILD_DIR/$series/"

    if [ "$UPLOAD" = "--upload" ]; then
        dput "$PPA" "$changes"
    fi
done

echo
echo "Source packages ready in $BUILD_DIR/<series>/"
find "$BUILD_DIR" -maxdepth 2 -name '*.changes'
