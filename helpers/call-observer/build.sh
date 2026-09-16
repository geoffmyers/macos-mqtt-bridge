#!/usr/bin/env bash
# Build the CallObserver helper. Output binary lands at .build/release/CallObserver.
# The Python bridge spawns this binary as a subprocess.

set -euo pipefail

HELPER_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "${HELPER_DIR}"

if ! command -v swift >/dev/null 2>&1; then
    echo "missing: swift — install Xcode command-line tools with: xcode-select --install" >&2
    exit 1
fi

echo "==> building CallObserver"
# CallKit (CXCallObserver) is iOS-only on the macOS SDK, so build for the
# Mac Catalyst target — Catalyst exposes CallKit and the resulting Mach-O
# arm64 binary still runs as a plain CLI process on macOS.
CATALYST_TARGET="${CATALYST_TARGET:-arm64-apple-ios17.0-macabi}"
swift build -c release -Xswiftc -target -Xswiftc "${CATALYST_TARGET}"

BIN="${HELPER_DIR}/.build/release/CallObserver"
if [[ ! -x "${BIN}" ]]; then
    echo "build did not produce ${BIN}" >&2
    exit 1
fi

echo "==> built: ${BIN}"
echo "    test interactively with: ${BIN}"
