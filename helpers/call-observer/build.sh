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
# --triple, not -Xswiftc -target: Swift 6.4 (Xcode 27) ignores the -Xswiftc form and builds for
# macOS, where CXCallObserver is unavailable. The product's location also moved under 6.4's build
# system (.build/out/Products/Release-maccatalyst, while --show-bin-path still reports
# Products/Release), so find it and copy it to the .build/release/CallObserver path the bridge's
# config points at.
swift build -c release --triple "${CATALYST_TARGET}"

BIN="${HELPER_DIR}/.build/release/CallObserver"
BUILT="$(find .build -path '*maccatalyst*' -name CallObserver -type f -perm -u+x | head -1)"
if [[ -n "${BUILT}" && "${BUILT}" != ".build/release/CallObserver" ]]; then
    mkdir -p "$(dirname "${BIN}")"
    cp -f "${BUILT}" "${BIN}"
fi
if [[ ! -x "${BIN}" ]]; then
    echo "build did not produce ${BIN}" >&2
    exit 1
fi

echo "==> built: ${BIN}"
echo "    test interactively with: ${BIN}"
