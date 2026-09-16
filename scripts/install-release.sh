#!/bin/sh
# Install a published Dropin zipapp after verifying its release checksum.
# Download this file first; do not pipe remote content directly to a shell.
set -eu

VERSION=${DROPIN_VERSION:-0.1.0}
BASE_URL=${DROPIN_RELEASE_BASE_URL:-https://github.com/jmller/dropin/releases/download}
TARGET=${DROPIN_TARGET:-"$HOME/.local/bin/dropin"}
ARTIFACT="dropin-$VERSION.pyz"
URL="$BASE_URL/v$VERSION"

usage() {
    cat <<EOF
Usage: scripts/install-release.sh

Downloads and verifies Dropin v$VERSION, then installs it at $TARGET.
Override with DROPIN_VERSION, DROPIN_RELEASE_BASE_URL, or DROPIN_TARGET.
EOF
}

case "${1-}" in
    "") ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { usage >&2; exit 2; }

command -v curl >/dev/null 2>&1 || { echo "dropin installer: curl is required" >&2; exit 2; }
command -v shasum >/dev/null 2>&1 || { echo "dropin installer: shasum is required" >&2; exit 2; }
command -v install >/dev/null 2>&1 || { echo "dropin installer: install is required" >&2; exit 2; }
command -v mv >/dev/null 2>&1 || { echo "dropin installer: mv is required" >&2; exit 2; }

TARGET=$(printf '%s' "$TARGET" | sed "s#^~/#$HOME/#")
target_dir=$(dirname "$TARGET")
mkdir -p "$target_dir"
temporary=$(mktemp -d "${TMPDIR:-/tmp}/dropin-release.XXXXXX")
staged=""
cleanup() {
    rm -rf "$temporary"
    [ -z "$staged" ] || rm -f "$staged"
}
trap cleanup EXIT HUP INT TERM

printf 'Downloading Dropin v%s...\n' "$VERSION"
curl --fail --location --silent --show-error --retry 3 \
    -o "$temporary/$ARTIFACT" "$URL/$ARTIFACT"
curl --fail --location --silent --show-error --retry 3 \
    -o "$temporary/SHA256SUMS" "$URL/SHA256SUMS"

(
    cd "$temporary"
    shasum -a 256 -c SHA256SUMS
)

staged=$(mktemp "$target_dir/.dropin-install.XXXXXX")
install -m 0755 "$temporary/$ARTIFACT" "$staged"
mv -f "$staged" "$TARGET"
printf 'Installed dropin at %s\n' "$TARGET"
case ":${PATH-}:" in
    *:"$target_dir":*) ;;
    *) printf 'Add %s to PATH if needed.\n' "$target_dir" ;;
esac
