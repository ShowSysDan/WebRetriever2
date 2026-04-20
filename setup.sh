#!/bin/bash
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="ndi-streamer"
INSTALL_DIR="/opt/ndi-streamer"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

echo ""
echo "========================================="
echo "  NDI Streamer — Setup"
echo "  Version: $(cat "$APP_DIR/VERSION")"
echo "========================================="
echo ""

# ------------------------------------------------------------------
# 1. Python check
# ------------------------------------------------------------------
PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" --version 2>&1 | grep -oP '\d+\.\d+')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    err "Python 3.10+ is required. Install it with:"
    echo "    sudo apt install python3.10 python3.10-venv"
    exit 1
fi
ok "Python found: $($PYTHON --version)"

# ------------------------------------------------------------------
# 2. Virtual environment
# ------------------------------------------------------------------
if [ ! -d "$APP_DIR/venv" ]; then
    info "Creating virtual environment..."
    $PYTHON -m venv "$APP_DIR/venv"
fi
source "$APP_DIR/venv/bin/activate"
ok "Virtual environment active"

# ------------------------------------------------------------------
# 3. Python dependencies
# ------------------------------------------------------------------
info "Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r "$APP_DIR/requirements.txt" -q
ok "Core dependencies installed"

# ndi-python install is deferred to after NDI SDK detection (section 5)
# because the build needs NDI_SDK_DIR pointing at the extracted SDK.

# ------------------------------------------------------------------
# 4. Playwright (headless Chromium)
# ------------------------------------------------------------------
info "Installing Playwright Chromium..."
playwright install chromium 2>/dev/null || true

# install-deps needs root to apt-install Chromium's shared libraries
# (libnss3, libatk-bridge2.0-0, libasound2, libxkbcommon0, etc.).
# On Debian trixie the playwright-supplied ubuntu package list pulls in
# ttf-ubuntu-font-family / ttf-unifont which no longer exist; fall back
# to installing the core runtime libs directly.
install_chromium_libs_fallback() {
    local runner="$1"
    $runner apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
        libxkbcommon0 libpango-1.0-0 libcairo2 libasound2t64 \
        libatspi2.0-0t64 libx11-6 libxcb1 libxext6 libdrm2 \
        fonts-liberation fonts-unifont 2>/dev/null
}

if [ "$EUID" -eq 0 ]; then
    if ! playwright install-deps chromium 2>/dev/null; then
        warn "playwright install-deps failed — trying fallback package list"
        install_chromium_libs_fallback "" || \
            warn "Fallback install failed — Chromium may be missing shared libs"
    fi
else
    info "Installing Chromium system libraries (requires sudo)..."
    if command -v sudo &>/dev/null; then
        if ! sudo "$(command -v playwright)" install-deps chromium 2>/dev/null; then
            warn "playwright install-deps failed — trying fallback package list"
            install_chromium_libs_fallback "sudo" || \
                warn "Fallback install failed — run manually: sudo $(command -v playwright) install-deps chromium"
        fi
    else
        warn "sudo not found. Run manually as root: $(command -v playwright) install-deps chromium"
    fi
fi
ok "Playwright ready"

# ------------------------------------------------------------------
# 5. NDI SDK detection + system install
# ------------------------------------------------------------------
echo ""

# Map machine arch -> NDI SDK's lib/<triple> directory
case "$(uname -m)" in
    x86_64)  NDI_SDK_ARCH="x86_64-linux-gnu" ;;
    i?86)    NDI_SDK_ARCH="i686-linux-gnu" ;;
    aarch64) NDI_SDK_ARCH="aarch64-rpi4-linux-gnueabi" ;;
    armv7l)  NDI_SDK_ARCH="arm-rpi3-linux-gnueabihf" ;;
    *)       NDI_SDK_ARCH="x86_64-linux-gnu" ;;
esac

# Find an extracted NDI SDK (v6 installer drops it at ~/"NDI SDK for Linux").
NDI_SDK_ROOT=""
_ndi_roots=(
    "${NDI_SDK_DIR:-}"
    "$HOME/NDI SDK for Linux"
    "/opt/NDI SDK for Linux"
    "/usr/local/NDI SDK for Linux"
)
if [ -n "${SUDO_USER:-}" ]; then
    _sudo_home="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6)"
    [ -n "$_sudo_home" ] && _ndi_roots+=("$_sudo_home/NDI SDK for Linux")
fi
for root in "${_ndi_roots[@]}"; do
    [ -z "$root" ] && continue
    if [ -f "$root/include/Processing.NDI.Lib.h" ] && \
       [ -f "$root/lib/$NDI_SDK_ARCH/libndi.so" ]; then
        NDI_SDK_ROOT="$root"
        ok "NDI SDK found at: $root (arch: $NDI_SDK_ARCH)"
        break
    fi
done

# Is the runtime lib already reachable via the linker?
NDI_RUNTIME_FOUND=false
if command -v ldconfig &>/dev/null && ldconfig -p | grep -q 'libndi\.so'; then
    NDI_RUNTIME_FOUND=true
fi
for ndi_path in /usr/share/ndi/lib /usr/local/lib /usr/lib /opt/ndi/lib \
                /usr/lib/x86_64-linux-gnu /usr/lib/aarch64-linux-gnu; do
    if ls "$ndi_path"/libndi.so* &>/dev/null 2>&1; then
        NDI_RUNTIME_FOUND=true
        break
    fi
done

# If we found an extracted SDK but no system-installed runtime, copy it
# into /usr/local so the ndi-streamer service user can load it under
# ProtectSystem=strict (which leaves /usr/local writable+readable).
install_ndi_system() {
    local runner=""
    [ "$EUID" -ne 0 ] && runner="sudo"
    info "Installing NDI runtime libs to /usr/local/lib (requires root)..."
    $runner cp -df "$NDI_SDK_ROOT/lib/$NDI_SDK_ARCH"/libndi.so* /usr/local/lib/ || return 1
    $runner mkdir -p /usr/local/include/ndi
    $runner cp -rf "$NDI_SDK_ROOT/include/". /usr/local/include/ndi/ || return 1
    $runner ldconfig
    ok "NDI runtime installed to /usr/local/lib"
    NDI_RUNTIME_FOUND=true
}

if [ -n "$NDI_SDK_ROOT" ] && [ "$NDI_RUNTIME_FOUND" = false ]; then
    if [ "$EUID" -eq 0 ] || command -v sudo &>/dev/null; then
        install_ndi_system || warn "NDI system install failed — continuing in dummy mode"
    else
        warn "Run as root (or install sudo) to copy libndi into /usr/local/lib"
    fi
fi

if [ "$NDI_RUNTIME_FOUND" = false ] && [ -z "$NDI_SDK_ROOT" ]; then
    warn "NDI SDK runtime NOT detected."
    echo ""
    echo "  The app will run in dummy mode (no NDI output) until the SDK is installed."
    echo ""
    echo "  To install the NDI SDK:"
    echo "    1. Download from: https://ndi.video/tools/ndi-sdk/"
    echo "    2. Run the installer (extracts SDK to ~/NDI SDK for Linux):"
    echo "       chmod +x Install_NDI_SDK_v6_Linux.sh"
    echo "       ./Install_NDI_SDK_v6_Linux.sh"
    echo "    3. Re-run ./setup.sh — it will pick up the extracted SDK and"
    echo "       install the libs to /usr/local/lib automatically."
    echo ""
fi

# ------------------------------------------------------------------
# 5b. Optional: NDI Python bindings (now that the SDK is known)
# ------------------------------------------------------------------
# ndi-python has no 3.13 wheels yet and must be compiled from source.
# The build needs cmake + a C++ toolchain; install them on-demand.
if [ -n "$NDI_SDK_ROOT" ]; then
    if ! command -v cmake &>/dev/null || ! command -v c++ &>/dev/null; then
        info "Installing build tools for ndi-python (cmake, gcc, ninja)..."
        if [ "$EUID" -eq 0 ]; then
            apt-get install -y --no-install-recommends \
                build-essential cmake ninja-build python3-dev 2>/dev/null || \
                warn "apt install of build tools failed — ndi-python may not build"
        elif command -v sudo &>/dev/null; then
            sudo apt-get install -y --no-install-recommends \
                build-essential cmake ninja-build python3-dev 2>/dev/null || \
                warn "apt install of build tools failed — ndi-python may not build"
        else
            warn "cmake is required to build ndi-python — install build-essential cmake ninja-build"
        fi
    fi
fi

info "Installing ndi-python (optional)..."
if [ -n "$NDI_SDK_ROOT" ]; then
    export NDI_SDK_DIR="$NDI_SDK_ROOT"
fi
if pip install ndi-python 2>&1 | tee /tmp/ndi-python-install.log | tail -3 | grep -q "Successfully installed"; then
    ok "ndi-python installed"
elif pip show ndi-python &>/dev/null; then
    ok "ndi-python already installed"
else
    warn "ndi-python could not be installed — app will run in dummy mode"
    warn "  See /tmp/ndi-python-install.log for the build error"
    if [ -z "$NDI_SDK_ROOT" ]; then
        warn "  (no NDI SDK was found for the build to link against)"
    fi
fi

# ------------------------------------------------------------------
# 6. Environment file
# ------------------------------------------------------------------
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    ok "Created .env from .env.example"
    warn "Edit .env to set SECRET_KEY and other options before production use."
else
    ok ".env already exists"
fi

# ------------------------------------------------------------------
# 7. Initialize database
# ------------------------------------------------------------------
info "Initializing database..."
cd "$APP_DIR"
$PYTHON -c "from app import create_app; create_app()" 2>/dev/null
ok "Database ready"

# ------------------------------------------------------------------
# 8. Service installation (optional)
# ------------------------------------------------------------------
echo ""
echo "-----------------------------------------"
echo "  Install as systemd service?"
echo "-----------------------------------------"
echo ""
echo "  This will:"
echo "    - Create a '${SERVICE_NAME}' system user"
echo "    - Copy files to ${INSTALL_DIR}"
echo "    - Install a systemd service that starts on boot"
echo "    - Auto-restart on crash"
echo ""
read -p "  Install as service? [y/N] " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    if [ "$EUID" -ne 0 ]; then
        err "Service installation requires root. Run with sudo:"
        echo "    sudo ./setup.sh"
        exit 1
    fi

    # Create system user
    if ! id "$SERVICE_NAME" &>/dev/null; then
        useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_NAME"
        ok "Created system user: $SERVICE_NAME"
    fi

    # Copy to install directory
    info "Copying to ${INSTALL_DIR}..."
    mkdir -p "$INSTALL_DIR"
    rsync -a --exclude='.git' --exclude='__pycache__' "$APP_DIR/" "$INSTALL_DIR/"

    # Recreate venv in install dir if different
    if [ "$APP_DIR" != "$INSTALL_DIR" ]; then
        info "Setting up venv in ${INSTALL_DIR}..."
        $PYTHON -m venv "$INSTALL_DIR/venv"
        source "$INSTALL_DIR/venv/bin/activate"
        pip install --upgrade pip -q
        pip install -r "$INSTALL_DIR/requirements.txt" -q
        if [ -n "$NDI_SDK_ROOT" ]; then
            NDI_SDK_DIR="$NDI_SDK_ROOT" pip install ndi-python -q 2>/dev/null || \
                warn "ndi-python build failed in service venv — service will run dummy mode"
        else
            pip install ndi-python -q 2>/dev/null || true
        fi
    fi

    # Install Playwright browsers into a shared path the service user can read.
    # The service user has --no-create-home and ProtectHome=true, so the
    # default ~/.cache/ms-playwright location is unreachable.
    info "Installing Playwright Chromium into ${INSTALL_DIR}/ms-playwright..."
    export PLAYWRIGHT_BROWSERS_PATH="$INSTALL_DIR/ms-playwright"
    mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
    "$INSTALL_DIR/venv/bin/playwright" install chromium 2>/dev/null || \
        warn "Failed to install Chromium into $PLAYWRIGHT_BROWSERS_PATH"
    unset PLAYWRIGHT_BROWSERS_PATH

    # Set ownership (includes the freshly-installed browsers)
    chown -R "$SERVICE_NAME:$SERVICE_NAME" "$INSTALL_DIR"

    # Verify service user can execute the venv Python
    if ! sudo -u "$SERVICE_NAME" test -x "$INSTALL_DIR/venv/bin/python"; then
        err "Service user '$SERVICE_NAME' cannot execute $INSTALL_DIR/venv/bin/python"
        err "Check permissions: ls -la $INSTALL_DIR/venv/bin/python"
        exit 1
    fi
    ok "Service user permissions verified"

    # Install service
    cp "$INSTALL_DIR/ndi-streamer.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    ok "Service installed and enabled"

    echo ""
    echo "  Manage with:"
    echo "    sudo systemctl start $SERVICE_NAME"
    echo "    sudo systemctl stop $SERVICE_NAME"
    echo "    sudo systemctl status $SERVICE_NAME"
    echo "    sudo journalctl -u $SERVICE_NAME -f"
    echo ""
    warn "Firewall reminder: open 5000/tcp (web UI), 5960-5969/tcp (NDI),"
    warn "and 5353/udp (mDNS discovery) if clients are on another subnet."
    warn "The web UI has no built-in authentication — restrict access by"
    warn "firewall, or place it behind nginx with basic auth (see README)."
    echo ""
else
    echo ""
    info "Skipping service installation."
    echo ""
    echo "  To run manually:"
    echo "    cd $APP_DIR"
    echo "    source venv/bin/activate"
    echo "    python run.py"
fi

echo ""
echo "========================================="
echo "  Setup complete!"
echo "  Open: http://localhost:5000"
echo "========================================="
echo ""
