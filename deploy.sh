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

if [ -z "$CAMERA_MODE" ]; then
    info "Checking non-interactive update permissions..."
    if ! ssh_cmd "sudo -n -l -- install -m 644 '$PI_DIR/services/titpi-identification.service' /etc/systemd/system/titpi-identification.service && \
                  sudo -n -l -- systemctl daemon-reload && \
                  sudo -n -l -- systemctl enable titpi-identification.service && \
                  sudo -n -l -- systemctl restart titpi.service titpi-web.service titpi-identification.service" >/dev/null 2>&1; then
        echo "ERROR: Cannot verify non-interactive sudo permissions for deployment."
        echo "No files synced and no services restarted by this run."
        echo "Ask the Pi administrator to authorize the exact deployment commands,"
        echo "or perform the privileged steps in an interactive SSH session."
        echo "See services/README.md: Deployment Sudo Permissions."
        exit 1
    fi
fi

# --- 2. Sync code ---
info "Syncing code to $PI_HOST:$PI_DIR..."
rsync -az \
    --exclude=.git/ \
    --exclude=.venv/ \
    --exclude=__pycache__/ \
    --exclude='*.pyc' \
    --exclude=detections/ \
    --exclude=config.json \
    --exclude='config.json.*' \
    --exclude=config_lcd.json \
    --exclude='config_lcd.json.*' \
    --exclude='.titpi-settings-*' \
    --progress \
    "$LOCAL_DIR/" "$PI_USER@$PI_HOST:$PI_DIR/"
ok "Code synced"

if [[ -f "$LOCAL_DIR/config_lcd.json" ]]; then
    info "Initializing matrix config only if absent on the Pi..."
    rsync -az --ignore-existing "$LOCAL_DIR/config_lcd.json" "$PI_USER@$PI_HOST:$PI_DIR/"
fi

# If no mode specified, just restart services and exit (update mode)
if [ -z "$CAMERA_MODE" ]; then
    info "Validating Python files before restart..."
    ssh_cmd "cd '$PI_DIR' && python3 -m py_compile bird_id.py watcher.py web.py notifier.py notifier_lcd.py hermes_ssh.py identification_queue.py identification_worker.py"
    info "Installing persistent identification worker..."
    ssh_cmd "sudo -n install -m 644 '$PI_DIR/services/titpi-identification.service' /etc/systemd/system/titpi-identification.service && sudo -n systemctl daemon-reload && sudo -n systemctl enable titpi-identification.service"
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
    ssh_cmd "sudo -n systemctl restart titpi.service titpi-web.service titpi-identification.service && systemctl is-active titpi.service titpi-web.service titpi-identification.service"
    ok "Services restarted. Done!"
    exit 0
fi

# --- Fresh install below ---
info "Fresh install (mode=$CAMERA_MODE)..."

# --- 3. Install system packages ---
info "Installing system packages..."
ssh_cmd "sudo apt update -qq && sudo apt install -y -qq \
    python3-picamera2 ffmpeg python3-flask python3-requests \
    python3-pil python3-numpy python3-libcamera openssh-client \
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
        "subject_prefix": "TitPi",
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
    "identification": {
        "provider": "none",
        "timeout": 30,
        "max_attempts": 2,
        "prompt": "Examine the attached image carefully. Is a bird visible? If so, identify which bird and species it is from visible features, giving its common name and scientific name only when supported by the image. Do not assume a bird is present or infer a species from the feeder or background. If blur, occlusion, or image quality prevents deciding whether a bird is present, use inconclusive rather than empty. Return ONLY a JSON object with status (empty, identified, inconclusive), common_name (string or null), name (Latin binomial or null), category (bird or unknown), confidence (0 to 1), and notes. Identify birds only; no bird means empty. For identified use category bird. If a bird is visible but its species is uncertain, use common_name \"Unidentified bird\" and name null; never invent a scientific name. For empty/inconclusive use null names, category unknown and confidence 0."
    },
    "hermes_ssh": {
        "host": "10.0.0.3",
        "user": "renanfernandes",
        "key_path": "/home/titpi/.ssh/id_ed25519_macmini",
        "remote_temp_dir": "/tmp",
        "hermes_path": "/Users/renanfernandes/.local/bin/hermes",
        "connect_timeout": 10,
        "timeout": 60,
        "max_attempts": 1,
        "min_confidence": 0.7,
        "notify_all": true
    },
    "openai": {
        "model": "gpt-4.1-mini",
        "api_key": ""
    },
    "claude": {
        "model": "claude-haiku-4-5",
        "api_key": ""
    },
    "database": {
        "web_host": "$PI_HOST",
        "web_port": 8080
    }
}
ENDCONFIG
    ok "config.json created — edit email and optional provider credentials on the Pi"
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
ssh_cmd "sudo cp $PI_DIR/services/titpi.service $PI_DIR/services/titpi-web.service $PI_DIR/services/titpi-identification.service $PI_DIR/services/titpi-botd.service $PI_DIR/services/titpi-botd.timer /etc/systemd/system/ && \
         sudo systemctl daemon-reload && \
         sudo systemctl enable titpi.service titpi-web.service titpi-identification.service titpi-botd.timer"
ok "Systemd services installed and enabled"

# --- 10. Start services ---
info "Starting services..."
ssh_cmd "sudo systemctl start titpi.service titpi-web.service titpi-identification.service titpi-botd.timer"
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
