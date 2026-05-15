#!/usr/bin/env bash
# ================================================================
# Goal Chat Agent — Deployment script
# ================================================================
set -euo pipefail

APP_DIR="/opt/goal"
PYTHON="${PYTHON:-python3}"
VENV="$APP_DIR/.venv"

echo "=== Goal Chat Agent Deployment ==="

# 1. Create directories
sudo mkdir -p "$APP_DIR" "$APP_DIR/data"
sudo chown -R "$USER:$USER" "$APP_DIR"

# 2. Copy files
echo "[1/5] Copying application files..."
cp -r "$(dirname "$0")"/* "$APP_DIR/"

# 3. Create virtual env
echo "[2/5] Setting up virtual environment..."
$PYTHON -m venv "$VENV"
source "$VENV/bin/activate"
pip install --upgrade pip
pip install -r "$APP_DIR/requirements.txt"

# 4. Create .env if not exists
if [ ! -f "$APP_DIR/.env" ]; then
    echo "[3/5] Creating .env from example..."
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "  ⚠️  Edit $APP_DIR/.env with your DeepSeek API key!"
fi

# 5. Install systemd service
echo "[4/5] Installing systemd service..."
sudo tee /etc/systemd/system/goal-agent.service > /dev/null <<'SYSTEMD'
[Unit]
Description=Goal Chat Agent
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/goal
EnvironmentFile=/opt/goal/.env
ExecStart=/opt/goal/.venv/bin/python main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SYSTEMD

sudo systemctl daemon-reload
sudo systemctl enable goal-agent

# 6. Start
echo "[5/5] Starting service..."
sudo systemctl restart goal-agent

echo "=== Done ==="
echo "Check status: sudo systemctl status goal-agent"
echo "View logs:   sudo journalctl -u goal-agent -f"
echo "Health check: curl http://localhost:8899/health"
