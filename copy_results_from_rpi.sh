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
# Uses rsync, so a repeat run only transfers what's new since the last one
# rather than the whole directory again. Nothing is removed on the Pi side;
# that's what --clean-results on main.py is for (see README.md).
# ---------------------------------------------------------------------------

SSH_TARGET="${1:?Error: SSH address required. Usage: $0 <user@rpi_ip> [local_dest]}"
LOCAL_DEST="${2:-results}"
RPI_USER="${SSH_TARGET%%@*}"
REMOTE_DIR="/home/${RPI_USER}/gora-testing-tool/results"
SSH_OPTS="-o ConnectTimeout=10 -o BatchMode=yes"

GREEN="\033[0;32m"
RED="\033[0;31m"
NC="\033[0m"

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

mkdir -p "$LOCAL_DEST"

info "Copying ${SSH_TARGET}:${REMOTE_DIR}/ to ${LOCAL_DEST}/"
# Trailing slash on the source copies the *contents* of results/ into
# LOCAL_DEST, rather than nesting a results/ subdirectory inside it.
rsync -avz --progress \
    -e "ssh ${SSH_OPTS}" \
    "${SSH_TARGET}:${REMOTE_DIR}/" \
    "${LOCAL_DEST}/" \
    || error "rsync failed - check the Pi is reachable and ${REMOTE_DIR} exists there."

info "Done. Results copied to ${LOCAL_DEST}/"
