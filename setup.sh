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
ok "Dependencies installed"

# ------------------------------------------------------------------
# 4. Playwright (headless Chromium)
# ------------------------------------------------------------------
info "Installing Playwright Chromium..."
playwright install chromium 2>/dev/null || true
playwright install-deps chromium 2>/dev/null || true
ok "Playwright ready"

# ------------------------------------------------------------------
# 5. NDI SDK detection
# ------------------------------------------------------------------
echo ""
NDI_FOUND=false
NDI_LIB_PATHS=(
    "/usr/share/ndi/lib"
    "/usr/local/lib"
    "/usr/lib"
    "/opt/ndi/lib"
)

for ndi_path in "${NDI_LIB_PATHS[@]}"; do
    if [ -f "$ndi_path/libndi.so" ] || ls "$ndi_path"/libndi.so.* &>/dev/null 2>&1; then
        NDI_FOUND=true
        ok "NDI SDK found at: $ndi_path"
        break
    fi
done

if [ "$NDI_FOUND" = false ]; then
    warn "NDI SDK runtime NOT detected."
    echo ""
    echo "  The app will run in dummy mode (no NDI output) until the SDK is installed."
    echo ""
    echo "  To install the NDI SDK:"
    echo "    1. Download from: https://ndi.video/tools/ndi-sdk/"
    echo "    2. Run the installer:"
    echo "       chmod +x Install_NDI_SDK_v6_Linux.sh"
    echo "       sudo ./Install_NDI_SDK_v6_Linux.sh"
    echo "    3. Add the library path to your environment:"
    echo "       echo 'export LD_LIBRARY_PATH=/usr/share/ndi/lib:\$LD_LIBRARY_PATH' >> ~/.bashrc"
    echo "       source ~/.bashrc"
    echo ""
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
        playwright install chromium 2>/dev/null || true
    fi

    # Set ownership
    chown -R "$SERVICE_NAME:$SERVICE_NAME" "$INSTALL_DIR"

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
