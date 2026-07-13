#!/bin/bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Usage: ./deploy_to_rpi.sh <user@rpi_ip>
#   user@rpi_ip  SSH address of the Raspberry Pi 4 (e.g. pi@192.168.1.42)
# ---------------------------------------------------------------------------

SSH_TARGET="${1:?Error: SSH address required. Usage: $0 <user@rpi_ip>}"
RPI_USER="${SSH_TARGET%%@*}"
RPI_IP="${SSH_TARGET##*@}"
REMOTE_DIR="/home/${RPI_USER}/gora-testing-tool"
VENV_DIR="${REMOTE_DIR}/.venv"
SSH_OPTS="-o ConnectTimeout=10 -o BatchMode=yes"
SSH="ssh ${SSH_OPTS} ${RPI_USER}@${RPI_IP}"

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
NC="\033[0m"

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
section() { echo -e "\n${GREEN}=== $* ===${NC}"; }

# ---------------------------------------------------------------------------
# 1. Check local dependencies
# ---------------------------------------------------------------------------
section "Checking local tools"

for tool in ssh rsync; do
    command -v "$tool" &>/dev/null || error "'$tool' not found on this machine. Please install it."
    info "$tool: OK"
done

# ---------------------------------------------------------------------------
# 2. Test SSH connectivity
# ---------------------------------------------------------------------------
section "Testing SSH connection to ${RPI_USER}@${RPI_IP}"

ssh ${SSH_OPTS} "${SSH_TARGET}" "exit 0" \
    || error "Cannot reach ${RPI_USER}@${RPI_IP}. Check the IP, SSH key, and that the Pi is online."
info "SSH connection: OK"

# ---------------------------------------------------------------------------
# 3. Check / install system packages on the Pi
# ---------------------------------------------------------------------------
section "Checking system packages on Pi"

check_apt_pkg() {
    local pkg="$1"
    $SSH "dpkg -l '$pkg' 2>/dev/null | grep -q '^ii'" && return 0 || return 1
}

MISSING_PKGS=()
for pkg in python3 python3-venv python3-pip; do
    if check_apt_pkg "$pkg"; then
        info "$pkg: already installed"
    else
        warn "$pkg: not found — will install"
        MISSING_PKGS+=("$pkg")
    fi
done

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    info "Installing: ${MISSING_PKGS[*]}"
    $SSH "sudo apt-get update -qq && sudo apt-get install -y -qq ${MISSING_PKGS[*]}"
    info "System packages installed"
fi

# ---------------------------------------------------------------------------
# 4. Sync project files
# ---------------------------------------------------------------------------
section "Syncing project files to ${REMOTE_DIR}"

rsync -avz --delete \
    --exclude="__pycache__/" \
    --exclude="*.pyc" \
    --exclude=".git/" \
    --exclude=".venv/" \
    --exclude="*.egg-info/" \
    -e "ssh ${SSH_OPTS}" \
    "$(dirname "$0")/" \
    "${RPI_USER}@${RPI_IP}:${REMOTE_DIR}/"

info "Files synced"

# ---------------------------------------------------------------------------
# 5. Create virtual environment (if not already present)
# ---------------------------------------------------------------------------
section "Setting up Python virtual environment"

if $SSH "test -f '${VENV_DIR}/bin/activate'"; then
    info "Virtual environment already exists at ${VENV_DIR}"
else
    info "Creating virtual environment at ${VENV_DIR}"
    $SSH "python3 -m venv '${VENV_DIR}'"
fi

# ---------------------------------------------------------------------------
# 6. Install / update Python packages
# ---------------------------------------------------------------------------
section "Installing Python packages"

$SSH "
    source '${VENV_DIR}/bin/activate'
    pip install --quiet --upgrade pip
    pip install --quiet -r '${REMOTE_DIR}/requirements.txt'
"

info "Python packages installed"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo -e "\n${GREEN}Deployment complete.${NC}"
echo -e "Run the tool on the Pi with:"
echo -e "  ssh ${RPI_USER}@${RPI_IP}"
echo -e "  source ${VENV_DIR}/bin/activate"
echo -e "  python ${REMOTE_DIR}/main.py -t ${REMOTE_DIR}/scenarios/test.yml"
