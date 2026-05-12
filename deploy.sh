#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------------
# Decky Plugin Deploy Script
# Builds locally on macOS, pushes to Steam Deck over SSH,
# and triggers an auto-reload via the debug flag.
#
# Usage:
#   ./deploy.sh              Build + deploy
#   ./deploy.sh --logs       Deploy then tail backend logs
#   ./deploy.sh --restart    Deploy then restart Steam (hard reload)
#   ./deploy.sh --watch      Rebuild + deploy on every file change
# ------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env.deck"

# ---- Load config ----
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found."
    echo "Copy .env.deck.example and fill in your Deck's IP."
    exit 1
fi
source "$ENV_FILE"

if [[ "$DECK_IP" == *"XXX"* ]]; then
    echo "ERROR: Update DECK_IP in .env.deck with your Steam Deck's actual IP address."
    echo "  On your Deck: Settings > Internet > your Wi-Fi > IP Address"
    exit 1
fi

REMOTE="${DECK_USER}@${DECK_IP}"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=5 -p ${DECK_PORT}"
SSH_TTY_OPTS="${SSH_OPTS} -t"
REMOTE_DIR="/home/${DECK_USER}/homebrew/plugins/${PLUGIN_NAME}"

# ---- Helper functions ----

log()  { echo -e "\033[1;34m==>\033[0m \033[1m$*\033[0m"; }
ok()   { echo -e "\033[1;32m  ✓\033[0m $*"; }
fail() { echo -e "\033[1;31m  ✗\033[0m $*"; exit 1; }

check_ssh() {
    log "Checking SSH connection to ${REMOTE}..."
    ssh $SSH_OPTS "$REMOTE" "echo ok" &>/dev/null \
        || fail "Cannot reach ${REMOTE}. Is SSH enabled on your Deck?\n  Run on Deck: sudo systemctl start sshd"
    ok "Connected to ${DECK_IP}"
}

build_frontend() {
    log "Building frontend..."
    cd "$SCRIPT_DIR"
    pnpm run build 2>&1 | tail -3
    ok "Frontend built (dist/index.js)"
}

ensure_debug_flag() {
    local pjson="$SCRIPT_DIR/plugin.json"
    if ! grep -q '"debug"' "$pjson"; then
        log "Injecting debug flag into plugin.json for auto-reload..."
        sed -i.bak 's/"flags": \[\]/"flags": ["debug"]/' "$pjson"
        sed -i.bak 's/"flags": \[\([^]]\)/"flags": ["debug", \1/' "$pjson"
        rm -f "${pjson}.bak"
        ok "Debug flag added (auto-reload enabled)"
    fi
}

deploy() {
    log "Deploying to ${REMOTE}:${REMOTE_DIR}..."

    # Stage in a temp dir the user owns, then swap into place
    local STAGING="/tmp/_decky_deploy_stage"
    ssh $SSH_OPTS "$REMOTE" "rm -rf ${STAGING} && mkdir -p ${STAGING}/dist ${STAGING}/py_modules"

    local SCP_OPTS="-q -o StrictHostKeyChecking=no -o ConnectTimeout=5 -P ${DECK_PORT}"

    # Copy plugin files to staging
    scp $SCP_OPTS \
        "$SCRIPT_DIR/plugin.json" \
        "$SCRIPT_DIR/package.json" \
        "$SCRIPT_DIR/main.py" \
        "${REMOTE}:${STAGING}/"
    scp $SCP_OPTS \
        "$SCRIPT_DIR/dist/index.js" \
        "$SCRIPT_DIR/dist/index.js.map" \
        "${REMOTE}:${STAGING}/dist/"
    scp $SCP_OPTS \
        "$SCRIPT_DIR/py_modules/__init__.py" \
        "$SCRIPT_DIR/py_modules/tv_client.py" \
        "${REMOTE}:${STAGING}/py_modules/"

    # Swap into plugin directory and restart Decky to reload the backend
    ssh $SSH_TTY_OPTS "$REMOTE" "sudo rm -rf '${REMOTE_DIR}' && sudo mv ${STAGING} '${REMOTE_DIR}' && sudo chmod -R 755 '${REMOTE_DIR}' && sudo systemctl restart plugin_loader"

    ok "Files synced, Decky restarted"
}

tail_logs() {
    local log_path="/home/${DECK_USER}/homebrew/logs/${PLUGIN_NAME}/plugin.log"
    log "Tailing backend logs (Ctrl+C to stop)..."
    echo "  Log file: ${log_path}"
    echo ""
    ssh $SSH_OPTS "$REMOTE" "tail -f '${log_path}' 2>/dev/null || echo 'Log file not found yet — run the plugin first.'"
}

restart_steam() {
    log "Restarting Steam on Deck..."
    ssh $SSH_OPTS "$REMOTE" "sudo systemctl restart steam" 2>/dev/null || true
    ok "Steam restart triggered"
}

# ---- Main ----

MODE="${1:-deploy}"

case "$MODE" in
    --logs|-l)
        check_ssh
        tail_logs
        ;;
    --restart|-r)
        check_ssh
        build_frontend
        ensure_debug_flag
        deploy
        restart_steam
        log "Done! Steam is restarting on your Deck."
        ;;
    --watch|-w)
        check_ssh
        log "Watch mode: will rebuild + deploy on every file change"
        log "Watching src/, main.py, defaults/ ..."
        echo ""

        # Initial deploy
        build_frontend
        ensure_debug_flag
        deploy
        ok "Initial deploy complete"
        echo ""

        # Watch for changes using fswatch (macOS) or fallback to polling
        if command -v fswatch &>/dev/null; then
            fswatch -o \
                "$SCRIPT_DIR/src" \
                "$SCRIPT_DIR/main.py" \
                "$SCRIPT_DIR/defaults" \
                "$SCRIPT_DIR/plugin.json" \
            | while read -r; do
                echo ""
                log "Change detected, rebuilding..."
                build_frontend
                deploy
                ok "Deployed at $(date +%H:%M:%S)"
            done
        else
            echo "TIP: Install fswatch for instant rebuilds:  brew install fswatch"
            echo "Falling back to 3-second polling..."
            echo ""
            LAST_HASH=""
            while true; do
                HASH=$(find "$SCRIPT_DIR/src" "$SCRIPT_DIR/main.py" "$SCRIPT_DIR/defaults" -type f -exec stat -f '%m' {} + 2>/dev/null | md5)
                if [[ "$HASH" != "$LAST_HASH" && -n "$LAST_HASH" ]]; then
                    echo ""
                    log "Change detected, rebuilding..."
                    build_frontend
                    deploy
                    ok "Deployed at $(date +%H:%M:%S)"
                fi
                LAST_HASH="$HASH"
                sleep 3
            done
        fi
        ;;
    *)
        check_ssh
        build_frontend
        ensure_debug_flag
        deploy
        echo ""
        log "Done! Plugin deployed."
        echo ""
        echo "  Your plugin should auto-reload in Game Mode."
        echo "  If it doesn't appear, go to Decky menu and check."
        echo ""
        echo "  Useful commands:"
        echo "    ./deploy.sh --logs      Tail backend Python logs"
        echo "    ./deploy.sh --watch     Auto-deploy on file changes"
        echo "    ./deploy.sh --restart   Deploy + restart Steam"
        echo ""
        ;;
esac
