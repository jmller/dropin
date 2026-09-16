#!/bin/sh
# Check Dropin's runtime prerequisites without changing the machine.
set -eu

PYTHON_MIN=3.11.0
RESTIC_MIN=0.19.1
RCLONE_MIN=1.75.1

usage() {
    cat <<'EOF'
Usage: scripts/install-prerequisites.sh

Checks Python 3.11+, restic 0.19.1+, and rclone 1.75.1+.
The script never installs software or changes the machine.
EOF
}

case "${1-}" in
    "") ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { usage >&2; exit 2; }

version_at_least() {
    awk -v found="$1" -v required="$2" 'BEGIN {
        pattern = "^[0-9][0-9]*\\.[0-9][0-9]*\\.[0-9][0-9]*$"
        if (found !~ pattern || required !~ pattern) exit 2
        split(found, f, "."); split(required, r, ".")
        for (i = 1; i <= 3; i++) {
            f[i] += 0; r[i] += 0
            if (f[i] > r[i]) exit 0
            if (f[i] < r[i]) exit 1
        }
        exit 0
    }'
}

python_version() {
    command -v python3 >/dev/null 2>&1 || return 1
    found=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' \
        2>/dev/null) || return 1
    version_at_least "$found" 0.0.0 || return 1
    printf '%s\n' "$found"
}

tool_version() {
    output=$("$2" version 2>/dev/null) || return 1
    first=$(printf '%s\n' "$output" | awk 'NR == 1 { print; exit }')
    case "$1:$first" in
        restic:restic\ [vV][0-9]*.[0-9]*.[0-9]*|restic:restic\ [0-9]*.[0-9]*.[0-9]*)
            found=$(printf '%s\n' "$first" | awk '{ v=$2; sub(/^[vV]/, "", v); print v }')
            ;;
        rclone:rclone\ v[0-9]*.[0-9]*.[0-9]*)
            found=$(printf '%s\n' "$first" | awk '{ v=$2; sub(/^v/, "", v); print v }')
            ;;
        *) return 1 ;;
    esac
    version_at_least "$found" 0.0.0 || return 1
    printf '%s\n' "$found"
}

check_python() {
    path=$(command -v python3 2>/dev/null || true)
    if [ -z "$path" ]; then
        echo "MISSING  python3 (required: $PYTHON_MIN+)" >&2
        return 1
    fi
    found=$(python_version) || {
        echo "INVALID  python3 at $path (cannot obtain a valid version)" >&2
        return 1
    }
    if ! version_at_least "$found" "$PYTHON_MIN"; then
        echo "OUTDATED python3 $found at $path (required: $PYTHON_MIN+)" >&2
        return 1
    fi
    echo "OK       python3 $found at $path"
}

check_tool() {
    tool=$1
    required=$2
    path=$(command -v "$tool" 2>/dev/null || true)
    if [ -z "$path" ]; then
        echo "MISSING  $tool (required: $required+)" >&2
        return 1
    fi
    found=$(tool_version "$tool" "$path") || {
        echo "INVALID  $tool at $path (cannot obtain a valid version)" >&2
        return 1
    }
    if ! version_at_least "$found" "$required"; then
        echo "OUTDATED $tool $found at $path (required: $required+)" >&2
        return 1
    fi
    echo "OK       $tool $found at $path"
}

status=0
check_python || status=1
check_tool restic "$RESTIC_MIN" || status=1
check_tool rclone "$RCLONE_MIN" || status=1

if [ "$status" -ne 0 ]; then
    cat >&2 <<'EOF'

Install missing prerequisites yourself, then rerun this check.
On macOS, Homebrew users can run: brew install python@3.11 restic rclone
Python is also available from: https://www.python.org/downloads/
EOF
fi

exit "$status"
