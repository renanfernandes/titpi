#!/bin/bash
# deploy.sh — Deploy TitPi to a Raspberry Pi (fresh install or update)
#
# Usage:
#   ./deploy.sh <pi-ip> [mode]
#
# Examples:
#   ./deploy.sh 10.0.1.60              # Update existing Pi (imx500)
#   ./deploy.sh 10.0.0.162 motion      # Fresh install with Camera Module 3
#   ./deploy.sh 10.0.1.60 imx500       # Fresh install with IMX500

set -euo pipefail

PI_HOST="${1:?Usage: ./deploy.sh <pi-ip> [mode]}"
CAMERA_MODE="${2:-}"   # empty = update only, "imx500" or "motion" = fresh setup
PI_USER="titpi"
PI_DIR="/home/$PI_USER/titpi"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${CYAN}[DEPLOY]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }

ssh_cmd() { ssh "$PI_USER@$PI_HOST" "$@"; }

# --- 1. Check SSH connectivity ---
info "Checking SSH to $PI_USER@$PI_HOST..."
if ! ssh_cmd "echo ok" >/dev/null 2>&1; then
    echo "ERROR: Cannot SSH to $PI_USER@$PI_HOST"
    echo "Make sure:"
    echo "  1. Pi is on and reachable at $PI_HOST"
    echo "  2. User '$PI_USER' exists on the Pi"
    echo "  3. SSH key is set up: ssh-copy-id $PI_USER@$PI_HOST"
    exit 1
fi
ok "SSH connected"

# --- 2. Sync code ---
info "Syncing code to $PI_HOST:$PI_DIR..."
rsync -az \
    --exclude=.git/ \
    --exclude=.venv/ \
    --exclude=__pycache__/ \
    --exclude='*.pyc' \
    --exclude=detections/ \
    --exclude=config.json \
    --progress \
    "$LOCAL_DIR/" "$PI_USER@$PI_HOST:$PI_DIR/"
ok "Code synced"

# If no mode specified, just restart services and exit (update mode)
if [ -z "$CAMERA_MODE" ]; then
    info "Update mode — updating web_host in config.json..."
    ssh_cmd "python3 -c \"
import json, sys
path = '$PI_DIR/config.json'
with open(path) as f: cfg = json.load(f)
cfg.setdefault('database', {})['web_host'] = '$PI_HOST'
with open(path, 'w') as f: json.dump(cfg, f, indent=4)
print('web_host set to $PI_HOST')
\""
    info "Restarting services..."
    ssh_cmd "sudo systemctl restart titpi.service titpi-web.service 2>/dev/null || true"
    ok "Services restarted. Done!"
    exit 0
fi

# --- Fresh install below ---
info "Fresh install (mode=$CAMERA_MODE)..."

# --- 3. Install system packages ---
info "Installing system packages..."
ssh_cmd "sudo apt update -qq && sudo apt install -y -qq \
    python3-picamera2 ffmpeg python3-flask python3-requests \
    python3-pil python3-numpy python3-libcamera \
    $([ '$CAMERA_MODE' = 'imx500' ] && echo 'imx500-all' || echo '') \
    2>&1 | tail -5"
ok "System packages installed"

# --- 4. Install Python packages ---
info "Installing ai-edge-litert (TFLite runtime)..."
ssh_cmd "pip install --break-system-packages --user ai-edge-litert 2>&1 | tail -3"
ok "Python packages installed"

# --- 5. Verify TFLite model files ---
info "Checking TFLite model files..."
if ssh_cmd "test -f $PI_DIR/aiy_birds_v1.tflite"; then
    ok "Model present"
else
    warn "aiy_birds_v1.tflite missing! Make sure it exists in your local repo."
    warn "Download it and place in: $LOCAL_DIR/aiy_birds_v1.tflite"
fi

# --- 6. Create config.json ---
info "Checking config.json..."
if ssh_cmd "test -f $PI_DIR/config.json"; then
    warn "config.json already exists — skipping (edit manually if needed)"
else
    info "Creating config.json for mode=$CAMERA_MODE..."
    ssh_cmd "cat > $PI_DIR/config.json" << ENDCONFIG
{
    "camera": {
        "mode": "$CAMERA_MODE",
        "model_path": "/usr/share/imx500-models/imx500_network_efficientdet_lite0_pp.rpk",
        "confidence_threshold": 0.55,
        "confirmation_frames": 3,
        "photo_delay": 1.5,
        "video_min_duration": 10,
        "video_max_duration": 120,
        "video_idle_timeout": 5,
        "photo_interval": 3,
        "cooldown": 15,
        "save_directory": "$PI_DIR/detections",
        "target_labels": ["bird", "person", "dog"],
        "spike_threshold": 0.20,
        "baseline_frames": 60,
        "motion_threshold": 5.0,
        "motion_min_area": 0.01
    },
    "email": {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "from": "",
        "password": "",
        "to": "",
        "attach_video": false
    },
    "local_classifier": {
        "enabled": true,
        "min_confidence": 0.3
    },
    "github": {
        "token": "",
        "model": "gpt-4o-mini",
        "identify_all": true
    },
    "database": {
        "web_host": "$PI_HOST",
        "web_port": 8080
    }
}
ENDCONFIG
    ok "config.json created — edit email/github credentials: ssh $PI_USER@$PI_HOST nano $PI_DIR/config.json"
fi

# --- 7. Create directories ---
info "Creating directories..."
ssh_cmd "mkdir -p $PI_DIR/detections/bird_of_the_day"
ok "Directories created"

# --- 8. Initialize database ---
info "Initializing database..."
ssh_cmd "cd $PI_DIR && python3 -c 'import database; database.init_db()'"
ok "Database initialized"

# --- 9. Install systemd services ---
info "Installing systemd services..."
ssh_cmd "sudo cp $PI_DIR/services/titpi.service $PI_DIR/services/titpi-web.service $PI_DIR/services/titpi-botd.service $PI_DIR/services/titpi-botd.timer /etc/systemd/system/ && \
         sudo systemctl daemon-reload && \
         sudo systemctl enable titpi.service titpi-web.service titpi-botd.timer"
ok "Systemd services installed and enabled"

# --- 10. Start services ---
info "Starting services..."
ssh_cmd "sudo systemctl start titpi.service titpi-web.service titpi-botd.timer"
ok "Services started"

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} TitPi deployed to $PI_HOST ($CAMERA_MODE)${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "  Dashboard: http://$PI_HOST:8080"
echo "  Logs:      ssh $PI_USER@$PI_HOST journalctl -u titpi -f"
echo ""
if ssh_cmd "grep '\"from\": \"\"' $PI_DIR/config.json" >/dev/null 2>&1; then
    warn "Email credentials not set — edit config.json on the Pi"
fi
if ssh_cmd "grep '\"token\": \"\"' $PI_DIR/config.json" >/dev/null 2>&1; then
    warn "GitHub token not set — edit config.json on the Pi (needed for GPT fallback)"
fi
