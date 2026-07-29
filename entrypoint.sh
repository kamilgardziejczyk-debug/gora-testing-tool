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
