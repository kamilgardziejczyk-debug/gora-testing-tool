#!/bin/bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Usage: ./copy_firmware_to_rpi.sh <user@rpi_ip> <archive>
#   user@rpi_ip  SSH address of the Raspberry Pi 4 (e.g. pi@192.168.1.42)
#   archive      Local path to a .zip, .tar.gz, or .tgz firmware archive
# ---------------------------------------------------------------------------

SSH_TARGET="${1:?Error: SSH address required. Usage: $0 <user@rpi_ip> <archive>}"
ARCHIVE="${2:?Error: Archive file required. Usage: $0 <user@rpi_ip> <archive>}"
RPI_USER="${SSH_TARGET%%@*}"
REMOTE_DIR="/home/${RPI_USER}/gora-testing-tool/firmware"
SSH_OPTS="-o ConnectTimeout=10 -o BatchMode=yes"
SSH="ssh ${SSH_OPTS} ${SSH_TARGET}"

GREEN="\033[0;32m"
RED="\033[0;31m"
NC="\033[0m"

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Validate archive
# ---------------------------------------------------------------------------
[ -f "$ARCHIVE" ] || error "File not found: $ARCHIVE"

ARCHIVE_NAME="$(basename "$ARCHIVE")"

case "$ARCHIVE_NAME" in
    *.tar.gz|*.tgz) EXTRACT_CMD="tar -xzf" ;;
    *.tar)          EXTRACT_CMD="tar -xf"  ;;
    *.zip)          EXTRACT_CMD="unzip -o" ;;
    *) error "Unsupported archive format: $ARCHIVE_NAME (supported: .zip, .tar, .tar.gz, .tgz)" ;;
esac

# ---------------------------------------------------------------------------
# Copy archive
# ---------------------------------------------------------------------------
info "Creating remote directory: ${REMOTE_DIR}"
$SSH "mkdir -p '${REMOTE_DIR}'"

info "Copying ${ARCHIVE_NAME} to ${SSH_TARGET}:${REMOTE_DIR}/"
rsync -avz --progress \
    -e "ssh ${SSH_OPTS}" \
    "$ARCHIVE" \
    "${SSH_TARGET}:${REMOTE_DIR}/"

# ---------------------------------------------------------------------------
# Extract on the Pi and remove the archive
# ---------------------------------------------------------------------------
info "Extracting ${ARCHIVE_NAME} on Pi"
$SSH "cd '${REMOTE_DIR}' && ${EXTRACT_CMD} '${ARCHIVE_NAME}' && rm '${ARCHIVE_NAME}'"

info "Done. Firmware extracted to ${REMOTE_DIR}/"
$SSH "ls -lh '${REMOTE_DIR}/'"
