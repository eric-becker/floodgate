#!/usr/bin/env bash
# Download the Meshtastic protobuf definitions at a pinned commit.
# Usage: ./scripts/download_protobufs.sh [target_dir]
#
# The commit is pinned so every build compiles the same schema: an unpinned
# clone of the default branch could change the generated stubs with no diff in
# this repo. Bump PROTOBUFS_REF deliberately, and run the full test suite.
# Override for a one-off experiment with PROTOBUFS_REF=<commit> in the env.

set -euo pipefail

PROTO_REPO="https://github.com/meshtastic/protobufs.git"
PROTOBUFS_REF="${PROTOBUFS_REF:-95c5f8c1c4223bb27faa81417b90560037f8ad31}"   # master, 2026-10-03
TARGET_DIR="${1:-protobufs}"

cd "$(dirname "$0")/.."

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "Fetching meshtastic/protobufs at ${PROTOBUFS_REF}..."
git -C "$WORK_DIR" init --quiet
git -C "$WORK_DIR" fetch --quiet --depth=1 "$PROTO_REPO" "$PROTOBUFS_REF"
git -C "$WORK_DIR" checkout --quiet FETCH_HEAD

# Replace only what the generator reads (plus the licence), so files tracked
# in this repo under the target directory are left alone.
mkdir -p "$TARGET_DIR"
rm -rf "$TARGET_DIR/meshtastic"
cp -R "$WORK_DIR/meshtastic" "$TARGET_DIR/meshtastic"
cp "$WORK_DIR/nanopb.proto" "$WORK_DIR/LICENSE" "$TARGET_DIR/"
echo "$PROTOBUFS_REF" > "$TARGET_DIR/.ref"

count=$(find "$TARGET_DIR/meshtastic" -name '*.proto' | wc -l)
echo "Protobufs at ${PROTOBUFS_REF} in ${TARGET_DIR} (${count} proto files)"
