#!/bin/bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Usage: GH_PAT=ghp_xxx ./deploy_docker_to_rpis.sh <target_user@ip>[:runner_name] [...]
#   target_user@ip  SSH address of a Raspberry Pi to load the built image
#                   onto (e.g. rpi1@192.168.1.42).
#   :runner_name    Optional. The runner's name/label on GitHub. Defaults to
#                   the part before the @, but several nodes sharing the same
#                   SSH login user (e.g. all logging in as "rpi") need this
#                   to tell them apart, since otherwise they'd all register
#                   under the same name and fight over it.
#                   Example: rpi@192.168.50.251:rpi-gpio
#
# Cross-builds the gora-testing-tool image for linux/arm64 once, here, on
# this machine (an x86_64 PC), streams it into `docker load` on every target
# Pi over a single SSH pipe per target, then starts each one as a GitHub
# Actions self-hosted runner - workflow job steps (e.g.
# `run: python main.py -t scenarios/gateway.yml`) then execute inside that
# same container. See entrypoint.sh.
#
# Env vars:
#   GH_PAT     (required) GitHub PAT used to mint each node's runner
#              registration token. Needs "Administration" write access on
#              GH_REPO (fine-grained PAT) or the classic `repo` scope.
#   GH_REPO    (optional) "owner/repo" to register runners against. Defaults
#              to this repo's own `origin` remote.
#   TZ         (optional) Passed into each container so its HTML reports use
#              local timestamps. Defaults to UTC.
#   EXTRA_DOCKER_RUN_ARGS (optional) Extra flags appended to every node's
#              `docker run`, e.g. '--device /dev/ttyUSB0' for a node that
#              also needs to drive serial scenarios. GPIO (/dev/gpiomem) and
#              BLE (the D-Bus socket) are already passed to every node by
#              default, since every Pi 4 test node has both; a serial
#              device path varies per node, so it isn't guessed for you.
#
# Requires Docker Buildx with QEMU emulation registered for arm64. One-time
# setup on a plain Linux Docker install:
#   docker run --privileged --rm tonistiigi/binfmt --install arm64
# Docker Desktop (Mac/Windows) bundles this already.
# ---------------------------------------------------------------------------

if [ "$#" -lt 1 ]; then
    echo "Usage: GH_PAT=ghp_xxx $0 <target_user@ip>[:runner_name] [...]" >&2
    exit 1
fi

TARGET_SSH_TARGETS=()
TARGET_LABELS=()
for raw_arg in "$@"; do
    TARGET_SSH_TARGETS+=("${raw_arg%%:*}")
    if [[ "$raw_arg" == *:* ]]; then
        TARGET_LABELS+=("${raw_arg#*:}")
    else
        TARGET_LABELS+=("${raw_arg%%@*}")
    fi
done

SSH_OPTS="-o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
IMAGE_NAME="gora-testing-tool"
CONTAINER_NAME="gora-node"
PLATFORM="linux/arm64"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
NC="\033[0m"

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
section() { echo -e "\n${GREEN}=== $* ===${NC}"; }

derive_gh_repo() {
    local url
    url="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
    url="${url%.git}"
    case "$url" in
        https://github.com/*) echo "${url#https://github.com/}" ;;
        git@github.com:*)     echo "${url#git@github.com:}" ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# 1. Check local dependencies and required env vars
# ---------------------------------------------------------------------------
section "Checking local tools"

for tool in docker ssh git; do
    command -v "$tool" &>/dev/null || error "'$tool' not found on this machine. Please install it."
    info "$tool: OK"
done

docker buildx version &>/dev/null \
    || error "Docker Buildx is required to cross-build for ${PLATFORM}. Install/enable it, then re-run."
info "docker buildx: OK"

if ! docker buildx inspect --bootstrap 2>&1 | grep -q "${PLATFORM}"; then
    warn "The active buildx builder doesn't report ${PLATFORM} support."
    warn "If the build below fails, register QEMU emulation once with:"
    warn "  docker run --privileged --rm tonistiigi/binfmt --install arm64"
fi

[ -n "${GH_PAT:-}" ] || error "GH_PAT env var must be set to a GitHub PAT (see the header of this script for required permissions)."

GH_REPO="${GH_REPO:-$(derive_gh_repo || true)}"
[ -n "${GH_REPO:-}" ] || error "Could not derive owner/repo from 'origin'; set GH_REPO=owner/repo explicitly."
info "Runners will register against: ${GH_REPO}"

RUN_TZ="${TZ:-UTC}"

# ---------------------------------------------------------------------------
# 2. Work out the image tag
# ---------------------------------------------------------------------------
section "Determining image tag"

TAG="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo "latest")"
if ! git -C "$REPO_DIR" diff --quiet 2>/dev/null || ! git -C "$REPO_DIR" diff --cached --quiet 2>/dev/null; then
    TAG="${TAG}-dirty"
    warn "Working tree has uncommitted changes; tagging as ${TAG}"
fi
info "Image tag: ${IMAGE_NAME}:${TAG}"

# ---------------------------------------------------------------------------
# 3. Test SSH connectivity to every target
# ---------------------------------------------------------------------------
section "Testing SSH connections"

for ssh_target in "${TARGET_SSH_TARGETS[@]}"; do
    ssh ${SSH_OPTS} "${ssh_target}" "exit 0" \
        || error "Cannot reach ${ssh_target}. Check the IP, SSH key, and that the Pi is online."
    info "${ssh_target}: OK"
done

# ---------------------------------------------------------------------------
# 4. Install Docker on any target node that doesn't have it yet
# ---------------------------------------------------------------------------
section "Checking Docker on target nodes"

for ssh_target in "${TARGET_SSH_TARGETS[@]}"; do
    if ssh ${SSH_OPTS} "${ssh_target}" "command -v docker" &>/dev/null; then
        info "${ssh_target}: docker already installed"
        continue
    fi

    warn "${ssh_target}: docker not found, installing (curl | sh get.docker.com)"

    ssh ${SSH_OPTS} "${ssh_target}" "sudo -n true" 2>/dev/null \
        || error "${ssh_target}: docker is missing and sudo needs a password on this account (can't supply one non-interactively). Install manually: ssh ${ssh_target}, then 'curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker \$USER', log out and back in, then re-run this script."

    ssh ${SSH_OPTS} "${ssh_target}" "curl -fsSL https://get.docker.com | sudo sh" \
        || error "${ssh_target}: Docker install failed. Check network access to get.docker.com on the Pi and re-run."
    ssh ${SSH_OPTS} "${ssh_target}" "sudo usermod -aG docker \$(whoami)"

    info "${ssh_target}: docker installed (group membership applies on the next SSH login, which every later step in this script already opens fresh)"
done

# ---------------------------------------------------------------------------
# 5. Cross-build the image for arm64, here
# ---------------------------------------------------------------------------
section "Building ${IMAGE_NAME}:${TAG} for ${PLATFORM}"

docker buildx build --platform "${PLATFORM}" -t "${IMAGE_NAME}:${TAG}" --load "${REPO_DIR}"

info "Build complete (emulated arm64 build on this machine can take a while - this is expected)"

# ---------------------------------------------------------------------------
# 6. Stream the image to every target, then (re)start it as a runner
# ---------------------------------------------------------------------------
section "Distributing image and starting runners"

declare -A NODE_IPS

for i in "${!TARGET_SSH_TARGETS[@]}"; do
    ssh_target="${TARGET_SSH_TARGETS[$i]}"
    LABEL="${TARGET_LABELS[$i]}"
    REMOTE_DIR="/home/${ssh_target%%@*}/gora-testing-tool"

    info "Streaming ${IMAGE_NAME}:${TAG} to ${ssh_target}"
    docker save "${IMAGE_NAME}:${TAG}" | gzip -1 \
        | ssh ${SSH_OPTS} "${ssh_target}" "gunzip | docker load"
    info "${ssh_target}: image loaded"

    ssh ${SSH_OPTS} "${ssh_target}" "mkdir -p '${REMOTE_DIR}/firmware' '${REMOTE_DIR}/results'"

    # Env vars (including GH_PAT) go over stdin via --env-file rather than
    # on the command line, so the PAT doesn't end up in `ps` output or
    # shell history on either end.
    ENV_FILE_REMOTE="${REMOTE_DIR}/.runner.env"
    ssh ${SSH_OPTS} "${ssh_target}" "cat > '${ENV_FILE_REMOTE}'" <<EOF
GH_PAT=${GH_PAT}
GH_REPO=${GH_REPO}
RUNNER_NAME=${LABEL}
RUNNER_LABELS=${LABEL}
TZ=${RUN_TZ}
EOF
    ssh ${SSH_OPTS} "${ssh_target}" "chmod 600 '${ENV_FILE_REMOTE}'"

    info "Starting runner container on ${ssh_target} (label: ${LABEL})"
    ssh ${SSH_OPTS} "${ssh_target}" "
        docker rm -f '${CONTAINER_NAME}' >/dev/null 2>&1 || true
        docker run -d --name '${CONTAINER_NAME}' --restart unless-stopped \
            --env-file '${ENV_FILE_REMOTE}' \
            --device /dev/gpiomem \
            -v /var/run/dbus:/var/run/dbus \
            -v '${REMOTE_DIR}/firmware:/app/firmware:ro' \
            -v '${REMOTE_DIR}/results:/app/results' \
            ${EXTRA_DOCKER_RUN_ARGS:-} \
            '${IMAGE_NAME}:${TAG}'
        rm -f '${ENV_FILE_REMOTE}'
    " || error "${ssh_target}: failed to start the runner container - check 'docker logs ${CONTAINER_NAME}' there."

    NODE_IPS["${ssh_target}"]="$(ssh ${SSH_OPTS} "${ssh_target}" \
        "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' '${CONTAINER_NAME}'" 2>/dev/null || true)"

    info "${ssh_target}: runner container started"
done

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
section "Deployment complete"

info "${IMAGE_NAME}:${TAG} is running as a self-hosted runner on:"
for i in "${!TARGET_SSH_TARGETS[@]}"; do
    ssh_target="${TARGET_SSH_TARGETS[$i]}"
    label="${TARGET_LABELS[$i]}"
    container_ip="${NODE_IPS[${ssh_target}]}"
    if [ -n "$container_ip" ]; then
        info "  - ${ssh_target}  (runner: ${label})  ->  container IP: ${container_ip}"
    else
        info "  - ${ssh_target}  (runner: ${label})  ->  container IP: unavailable (check 'docker inspect ${CONTAINER_NAME}' on the node)"
    fi
done

warn "scenarios/ is baked into the image - a scenario edit needs a rebuild + redeploy, not just a file copy."
warn "GH_PAT is written into ${CONTAINER_NAME}'s env on each node and visible via 'docker inspect' there - use a PAT scoped to just this repo's runner administration."
warn "Node-specific hardware access (e.g. --device /dev/gpiomem, /dev/ttyUSB0) is not added automatically - pass it via EXTRA_DOCKER_RUN_ARGS, matching the single-node instructions in README.md."
