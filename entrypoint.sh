#!/bin/bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Two modes, chosen by whether GH_PAT/GH_REPO are set at `docker run` time:
#
# - Runner mode: register this container as a GitHub Actions self-hosted
#   runner for GH_REPO, then run it in the foreground. Workflow job steps
#   (e.g. `run: python main.py -t scenarios/gateway.yml`) execute inside this
#   same container, since that's how a self-hosted runner works - no
#   `docker exec` needed from outside.
# - One-shot mode (default, unchanged): exec `python main.py "$@"`, same as
#   before this existed.
# ---------------------------------------------------------------------------

RUNNER_DIR="/opt/actions-runner"

# Best-effort: power on the host's Bluetooth adapter for !BleCentral
# scenarios, over the D-Bus socket bind-mounted per README - this container
# never runs its own bluetoothd or touches the adapter directly. Skipped
# entirely on a node with no D-Bus socket mounted (bluetoothctl aborts
# outright with nothing to connect to, rather than failing quietly) or no
# adapter at all, and it can't reach an rfkill-blocked adapter (that needs
# host-side capabilities this container doesn't have) -
# deploy_docker_to_rpis.sh's Bluetooth provisioning step is the permanent,
# reboot-surviving fix for both cases.
if [ -S /var/run/dbus/system_bus_socket ]; then
    timeout 5s bluetoothctl power on >/dev/null 2>&1 || true
fi

if [ -n "${GH_PAT:-}" ] && [ -n "${GH_REPO:-}" ]; then
    RUNNER_NAME="${RUNNER_NAME:-$(hostname)}"
    RUNNER_LABELS="${RUNNER_LABELS:-self-hosted}"

    fetch_token() {
        curl -fsS -X POST \
            -H "Authorization: token ${GH_PAT}" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/repos/${GH_REPO}/actions/runners/registration-token" \
            | jq -r .token
    }

    echo "[entrypoint] Registering runner '${RUNNER_NAME}' (labels: ${RUNNER_LABELS}) for ${GH_REPO}"

    cd "$RUNNER_DIR"
    ./config.sh --url "https://github.com/${GH_REPO}" \
        --token "$(fetch_token)" \
        --name "$RUNNER_NAME" \
        --labels "$RUNNER_LABELS" \
        --unattended --replace

    remove_runner() {
        echo "[entrypoint] De-registering runner '${RUNNER_NAME}'"
        ./config.sh remove --token "$(fetch_token)" || true
    }
    trap 'remove_runner; exit 130' INT
    trap 'remove_runner; exit 143' TERM

    ./run.sh &
    wait $!
else
    exec python main.py "$@"
fi
