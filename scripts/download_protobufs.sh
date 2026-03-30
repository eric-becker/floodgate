#!/usr/bin/env bash
# Download Meshtastic protobuf definitions from GitHub.
# Usage: ./scripts/download_protobufs.sh [target_dir]

set -euo pipefail

PROTO_REPO="https://github.com/meshtastic/protobufs.git"
TARGET_DIR="${1:-protobufs}"

cd "$(dirname "$0")/.."

if [ -d "$TARGET_DIR/meshtastic" ]; then
    echo "Updating existing protobufs in $TARGET_DIR..."
    cd "$TARGET_DIR"
    git pull --ff-only
else
    echo "Downloading meshtastic protobufs to $TARGET_DIR..."
    rm -rf "$TARGET_DIR"
    git clone --depth=1 "$PROTO_REPO" "$TARGET_DIR"
fi

echo "Protobufs downloaded to $TARGET_DIR"
ls "$TARGET_DIR"/meshtastic/*.proto 2>/dev/null | wc -l | xargs -I{} echo "  {} proto files found"
