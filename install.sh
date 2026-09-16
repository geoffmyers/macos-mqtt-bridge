#!/usr/bin/env bash
# install.sh
# Idempotent installer for macos-mqtt-bridge LaunchAgent.
#
# Replaces the prior screen-time-ha-bridge and macos-comms-mqtt-bridge
# daemons with a single merged process. Run --uninstall to remove just
# this daemon's plist; the migration script handles tearing down the
# legacy two.

set -euo pipefail

BRIDGE_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LABEL="com.geoffmyers.macos-mqtt-bridge"
PLIST_SRC="${BRIDGE_DIR}/launchd/${LABEL}.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
CONFIG_FILE="${BRIDGE_DIR}/config.yaml"
ENV_FILE="${BRIDGE_DIR}/.env"
ENV_TPL="${BRIDGE_DIR}/.env.tpl"

uninstall() {
    echo "uninstalling ${LABEL}…"
    if launchctl list | grep -q "${LABEL}"; then
        launchctl bootout "gui/$(id -u)" "${PLIST_DST}" 2>/dev/null || true
    fi
    rm -f "${PLIST_DST}"
    echo "removed ${PLIST_DST}"
    echo "(left ${CONFIG_FILE} and ${ENV_FILE} in place — delete manually if desired)"
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || { echo "missing: $1"; exit 1; }
}

main() {
    if [[ "${1:-}" == "--uninstall" ]]; then
        uninstall
        return 0
    fi

    require_cmd uv
    # .env comes from 1Password when an .env.tpl is present, else it is the
    # plain file you wrote from .env.example.
    if [[ -f "${ENV_TPL}" ]]; then
        require_cmd op
    fi

    HOMEBREW_PYTHON="/opt/homebrew/bin/python3"
    if [[ ! -x "${HOMEBREW_PYTHON}" ]]; then
        echo "missing: ${HOMEBREW_PYTHON} — install with: brew install python" >&2
        exit 1
    fi

    echo "==> syncing dependencies via uv (using ${HOMEBREW_PYTHON})"
    # ha-mqtt-bridge-toolkit is a path dependency on the copy under
    # _shared/. uv project mode honors [tool.uv.sources] in
    # pyproject.toml, but `uv pip install -e .` does not always pick it up
    # in older uv versions — so install it explicitly first as a
    # belt-and-suspenders guard.
    TOOLKIT_DIR="${BRIDGE_DIR}/_shared/ha-mqtt-bridge-toolkit"
    (
        cd "${BRIDGE_DIR}" \
            && rm -rf .venv \
            && uv venv --python "${HOMEBREW_PYTHON}" \
            && uv pip install -e "${TOOLKIT_DIR}" \
            && uv pip install -e '.[dev]'
    )

    if [[ ! -f "${CONFIG_FILE}" ]]; then
        echo "==> ${CONFIG_FILE} not found; copying from config.example.yaml"
        cp "${BRIDGE_DIR}/config.example.yaml" "${CONFIG_FILE}"
        echo "==> EDIT ${CONFIG_FILE} and re-run install.sh to continue"
        exit 0
    fi

    if [[ -f "${ENV_TPL}" ]]; then
        echo "==> generating ${ENV_FILE} via op inject"
        op inject -f -i "${ENV_TPL}" -o "${ENV_FILE}"
    elif [[ ! -f "${ENV_FILE}" ]]; then
        echo "missing: ${ENV_FILE} — copy .env.example to .env and fill it in"; exit 1
    fi
    chmod 600 "${ENV_FILE}"

    echo "==> building Swift CallObserver helper (Mac Catalyst target)"
    if ! bash "${BRIDGE_DIR}/helpers/call-observer/build.sh"; then
        echo "==> WARNING: CallObserver build failed; realtime call events disabled."
        echo "    The post-hoc CallHistory.storedata events still fire normally."
    fi

    echo "==> building Swift LocationFetcher helper"
    if ! bash "${BRIDGE_DIR}/helpers/location-fetcher/build.sh"; then
        echo "==> WARNING: LocationFetcher build failed; location sensors disabled."
        echo "    The rest of the bridge will run normally."
    fi

    echo "==> initializing comms-source state to current max IDs (so first run does not replay history)"
    # config.example.yaml references ${DARWIN_USER_DIR} for the RMAdminStore
    # paths; the launcher resolves it via getconf at daemon launch, so set it
    # the same way here for the one-shot init-state invocation.
    DARWIN_USER_DIR="$(getconf DARWIN_USER_DIR)" \
        "${BRIDGE_DIR}/.venv/bin/python" -m macos_bridge --config "${CONFIG_FILE}" init-state

    echo "==> installing LaunchAgent plist to ${PLIST_DST}"
    mkdir -p "$(dirname "${PLIST_DST}")"
    sed -e "s|__BRIDGE_DIR__|${BRIDGE_DIR}|g" \
        -e "s|__HOME__|${HOME}|g" \
        "${PLIST_SRC}" > "${PLIST_DST}"

    # Wrap the python launch with env-var loading so the daemon sees:
    #   - MQTT credentials from .env
    #   - DARWIN_USER_DIR via getconf (launchd doesn't set this; only shells
    #     do). The bridge's config.yaml may reference ${DARWIN_USER_DIR} for
    #     RMAdminStore paths under the per-user darwin temp dir.
    LAUNCHER="${BRIDGE_DIR}/.venv/bin/macos-mqtt-bridge-launcher.sh"
    cat > "${LAUNCHER}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
set -a
source "${ENV_FILE}"
DARWIN_USER_DIR="\$(getconf DARWIN_USER_DIR)"
set +a
exec "${BRIDGE_DIR}/.venv/bin/python" -m macos_bridge --config "${CONFIG_FILE}" run
EOF
    chmod +x "${LAUNCHER}"

    /usr/libexec/PlistBuddy -c "Delete :ProgramArguments" "${PLIST_DST}" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "${PLIST_DST}"
    /usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string ${LAUNCHER}" "${PLIST_DST}"

    echo "==> bootstrapping LaunchAgent"
    launchctl bootout "gui/$(id -u)" "${PLIST_DST}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "${PLIST_DST}"
    launchctl kickstart -k "gui/$(id -u)/${LABEL}"

    echo
    echo "==> installed. logs at: ${HOME}/Library/Logs/macos-mqtt-bridge*.log"
    echo "==> grant Full Disk Access to ${BRIDGE_DIR}/.venv/bin/python in"
    echo "    System Settings → Privacy & Security → Full Disk Access"
    echo "    (required to read chat.db, CallHistory.storedata, FaceTime voicemail store,"
    echo "    knowledgeC.db, RMAdminStore-Local.sqlite, and AddressBook source DBs)"
}

main "$@"
