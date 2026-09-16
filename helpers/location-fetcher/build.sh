#!/usr/bin/env bash
# Build the LocationFetcher helper.
#
# CoreLocation in a CLI binary requires either an embedded Info.plist
# *and* an .app-style structure for macOS TCC to (a) show the GUI
# permission prompt the first time the binary asks for a location fix
# and (b) accept the binary as a grant target in System Settings →
# Privacy & Security → Location Services.
#
# Approach: produce both
#   .build/release/LocationFetcher                       — raw Mach-O CLI
#   .build/release/LocationFetcher.app/Contents/MacOS/LocationFetcher
# The .app wrapper is what TCC actually grants permission to. The Python
# bridge invokes `LocationFetcher.app/Contents/MacOS/LocationFetcher` so
# the grant carries through.

set -euo pipefail

HELPER_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "${HELPER_DIR}"

if ! command -v swift >/dev/null 2>&1; then
    echo "missing: swift — install Xcode command-line tools with: xcode-select --install" >&2
    exit 1
fi

INFO_PLIST="${HELPER_DIR}/Info.plist"
if [[ ! -f "${INFO_PLIST}" ]]; then
    echo "missing: ${INFO_PLIST}" >&2
    exit 1
fi

echo "==> building LocationFetcher"
# Embed the Info.plist into the binary too, so even running the raw CLI
# (e.g. for ad-hoc debugging) carries the NSLocationUsageDescription.
swift build -c release \
    -Xlinker -sectcreate \
    -Xlinker __TEXT \
    -Xlinker __info_plist \
    -Xlinker "${INFO_PLIST}"

BIN="${HELPER_DIR}/.build/release/LocationFetcher"
if [[ ! -x "${BIN}" ]]; then
    echo "build did not produce ${BIN}" >&2
    exit 1
fi

# ---- assemble .app bundle ---------------------------------------------------
APP_DIR="${HELPER_DIR}/.build/release/LocationFetcher.app"
APP_MACOS_DIR="${APP_DIR}/Contents/MacOS"
APP_BIN="${APP_MACOS_DIR}/LocationFetcher"

echo "==> assembling ${APP_DIR}"
rm -rf "${APP_DIR}"
mkdir -p "${APP_MACOS_DIR}"
cp "${BIN}" "${APP_BIN}"
cp "${INFO_PLIST}" "${APP_DIR}/Contents/Info.plist"
chmod +x "${APP_BIN}"

# Ad-hoc code-sign the .app so TCC can persistently associate the
# Location grant with the binary identity rather than a per-build path.
echo "==> ad-hoc code-signing the .app"
codesign --force --deep -s - "${APP_DIR}" >/dev/null 2>&1 || \
    echo "    (codesign warning suppressed; ad-hoc signature is best-effort)"

echo "==> built: ${APP_BIN}"
echo "    test interactively with: ${APP_BIN} --reverse-geocode --timeout 20"
echo
echo "    NOTE: macOS will prompt to grant Location Services on first run."
echo "    If no prompt appears, open System Settings → Privacy & Security →"
echo "    Location Services and toggle on the LocationFetcher entry"
echo "    (or click + and add ${APP_BIN})."
