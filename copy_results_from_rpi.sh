#!/bin/bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Usage: ./copy_results_from_rpi.sh <user@rpi_ip> [local_dest]
#   user@rpi_ip  SSH address of the Raspberry Pi (e.g. rpi@192.168.50.44)
#   local_dest   Local directory to copy into (default: ./results)
#
# Pulls everything under the node's results/ - HTML reports, the per-run
# .tool/.device/.mqtt/.cli/.combined logs, and any !DutStorage copy_from
# output - down to this machine.
#
# The self-hosted-runner container (gora-node, see deploy_docker_to_rpis.sh)
# is long-lived, so results live inside it rather than always being
# reliably reflected on the Pi's own filesystem. This script therefore
# does the copy in two hops:
#   1. `docker cp` on the Pi, container -> a staging dir under /tmp there.
#   2. rsync, that staging dir -> LOCAL_DEST on this machine.
# The staging dir is wiped before each `docker cp` so it always mirrors
# the container's current results exactly, never a stale prior run.
# Nothing is removed from the container itself; that's what
# --clean-results on main.py is for (see README.md).
# ---------------------------------------------------------------------------

SSH_TARGET="${1:?Error: SSH address required. Usage: $0 <user@rpi_ip> [local_dest]}"
LOCAL_DEST="${2:-results}"
CONTAINER_NAME="gora-node"
REMOTE_STAGING_DIR="/tmp/${CONTAINER_NAME}-results"
SSH_OPTS="-o ConnectTimeout=10 -o BatchMode=yes"

GREEN="\033[0;32m"
RED="\033[0;31m"
NC="\033[0m"

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

mkdir -p "$LOCAL_DEST"

info "Copying ${CONTAINER_NAME}:/app/results out to ${SSH_TARGET}:${REMOTE_STAGING_DIR}"
ssh ${SSH_OPTS} "${SSH_TARGET}" "
    rm -rf '${REMOTE_STAGING_DIR}' &&
    docker cp '${CONTAINER_NAME}:/app/results' '${REMOTE_STAGING_DIR}'
" || error "docker cp failed - check the Pi is reachable and the '${CONTAINER_NAME}' container is running there."

info "Copying ${SSH_TARGET}:${REMOTE_STAGING_DIR}/ to ${LOCAL_DEST}/"
# Trailing slash on the source copies the *contents* of the staging dir into
# LOCAL_DEST, rather than nesting a results/ subdirectory inside it.
rsync -avz --progress \
    -e "ssh ${SSH_OPTS}" \
    "${SSH_TARGET}:${REMOTE_STAGING_DIR}/" \
    "${LOCAL_DEST}/" \
    || error "rsync failed - check the Pi is reachable and ${REMOTE_STAGING_DIR} exists there."

info "Done. Results copied to ${LOCAL_DEST}/"
