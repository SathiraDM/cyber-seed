#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  install.sh  —  One-click installer for cyber-seed
#  Run this once on a fresh Linux server:
#
#    curl -sSL https://raw.githubusercontent.com/you/cyber-seed/main/install.sh | bash
#    — or —
#    chmod +x install.sh && sudo ./install.sh
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║        cyber-seed  —  installer          ║${NC}"
echo -e "${CYAN}║  Torrent → OneDrive/SharePoint pipeline  ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── Check we're root or have sudo ────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    SUDO="sudo"
    warn "Not running as root — using sudo."
else
    SUDO=""
fi

# ── OS + arch ─────────────────────────────────────────────────────────
OS=$(uname -s)
ARCH=$(uname -m)
[[ "$OS" != "Linux" ]] && error "This script is for Linux servers only."
info "Detected OS: $OS ($ARCH)"

# ── Helper: check if a command exists ────────────────────────────────
has_cmd() { command -v "$1" &>/dev/null; }

# ── Install Docker ────────────────────────────────────────────────────
install_docker() {
    if has_cmd docker; then
        DOCKER_VER=$(docker --version)
        success "Docker already installed: $DOCKER_VER"
        return
    fi

    info "Installing Docker ..."

    if has_cmd apt-get; then
        # Debian / Ubuntu
        $SUDO apt-get update -qq
        $SUDO apt-get install -y -qq ca-certificates curl gnupg lsb-release
        $SUDO install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        echo \
          "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
          https://download.docker.com/linux/ubuntu \
          $(lsb_release -cs) stable" \
          | $SUDO tee /etc/apt/sources.list.d/docker.list > /dev/null
        $SUDO apt-get update -qq
        $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    elif has_cmd yum || has_cmd dnf; then
        # RHEL / CentOS / Fedora
        PKG=$(has_cmd dnf && echo dnf || echo yum)
        $SUDO $PKG install -y -q yum-utils
        $SUDO yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
        $SUDO $PKG install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    elif has_cmd zypper; then
        # openSUSE
        $SUDO zypper install -y docker docker-compose

    else
        warn "Package manager not detected. Trying convenience script ..."
        curl -fsSL https://get.docker.com | $SUDO sh
    fi

    # Start and enable Docker
    $SUDO systemctl enable docker --now
    success "Docker installed."

    # Add current user to docker group so they don't need sudo
    if [[ -n "${SUDO_USER:-}" ]]; then
        $SUDO usermod -aG docker "$SUDO_USER"
        warn "Added $SUDO_USER to the 'docker' group. Log out and back in for this to take effect."
    fi
}

# ── Verify Docker Compose v2 ──────────────────────────────────────────
check_compose() {
    if docker compose version &>/dev/null; then
        COMPOSE_VER=$(docker compose version)
        success "Docker Compose v2 available: $COMPOSE_VER"
    else
        error "Docker Compose v2 not found. Please upgrade Docker (20.10+)."
    fi
}

# ── Create required directories ───────────────────────────────────────
create_dirs() {
    info "Creating directory structure ..."
    mkdir -p \
        config/qbittorrent \
        config/rclone \
        downloads/incomplete \
        downloads/completed \
        logs

    success "Directories ready."
}

# ── Make scripts executable ───────────────────────────────────────────
fix_permissions() {
    chmod +x scripts/on-complete.sh scripts/init-qbt.sh setup.sh start.sh stop.sh install.sh
    success "Script permissions set."
}

# ── Run ────────────────────────────────────────────────────────────────
install_docker
check_compose
create_dirs
fix_permissions

echo ""
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo -e "${GREEN}  Installation complete!${NC}"
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo ""
echo "  Next steps:"
echo -e "    1. ${YELLOW}./setup.sh${NC}   ← Configure OneDrive + credentials"
echo -e "    2. ${YELLOW}./start.sh${NC}   ← Launch the stack"
echo -e "    3. Open ${CYAN}http://$(hostname -I | awk '{print $1}'):8080${NC} in your browser"
echo ""
