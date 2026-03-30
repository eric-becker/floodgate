#!/usr/bin/env bash
# Generate Python protobuf stubs from .proto files.
# Run from the project root: ./scripts/generate_protos.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

PROTO_DIR="$PROJECT_ROOT/protobufs"
EXHOOK_DIR="$PROJECT_ROOT/proto/emqx"
OUT_DIR="$PROJECT_ROOT/generated"

if [ ! -d "$PROTO_DIR/meshtastic" ]; then
    echo "ERROR: Meshtastic protobufs not found at $PROTO_DIR/meshtastic/"
    echo "Run: ./scripts/download_protobufs.sh"
    exit 1
fi

mkdir -p "$OUT_DIR"

echo "Generating EMQX ExHook gRPC stubs..."
python -m grpc_tools.protoc \
    -I "$EXHOOK_DIR" \
    --python_out="$OUT_DIR" \
    --grpc_python_out="$OUT_DIR" \
    "$EXHOOK_DIR/exhook.proto"

echo "Generating Meshtastic protobuf stubs..."
python -m grpc_tools.protoc \
    -I "$PROTO_DIR" \
    --python_out="$OUT_DIR" \
    "$PROTO_DIR"/meshtastic/*.proto \
    "$PROTO_DIR/nanopb.proto"

# Create __init__.py files for proper imports
touch "$OUT_DIR/__init__.py"
mkdir -p "$OUT_DIR/meshtastic"
touch "$OUT_DIR/meshtastic/__init__.py"

echo "Protobuf stubs generated in $OUT_DIR/"
