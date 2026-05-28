#!/usr/bin/env bash
# ================================================================
# Goal Chat Agent — Deployment script
# ================================================================
set -euo pipefail

APP_DIR="/opt/goal"
PYTHON="${PYTHON:-python3}"
VENV="$APP_DIR/.venv"
RUN_USER="${RUN_USER:-www-data}"
RUN_GROUP="${RUN_GROUP:-www-data}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Goal Chat Agent Deployment ==="

# 1. Create directories
sudo mkdir -p "$APP_DIR" "$APP_DIR/data" "$APP_DIR/logs"
sudo chown -R "$USER:$USER" "$APP_DIR"

# 2. Copy files
echo "[1/5] Copying application files..."
tar \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='*/__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='.env' \
    --exclude='data/memory.db' \
    --exclude='data/agent.log' \
    -C "$SRC_DIR" -cf - . | sudo tar -C "$APP_DIR" -xf -

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

sudo mkdir -p "$APP_DIR/data" "$APP_DIR/logs"
sudo touch "$APP_DIR/data/agent.log"
sudo chown -R "$RUN_USER:$RUN_GROUP" "$APP_DIR/data" "$APP_DIR/logs"
sudo chmod 750 "$APP_DIR/data" "$APP_DIR/logs"
sudo chmod 640 "$APP_DIR/data/agent.log"

# 5. Install systemd service
echo "[4/5] Installing systemd service..."
sudo tee /etc/systemd/system/goal-agent.service > /dev/null <<SYSTEMD
[Unit]
Description=Goal Chat Agent
After=network.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
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

for i in {1..10}; do
    if curl -fsS http://localhost:8899/health > /dev/null; then
        echo "Health check passed"
        break
    fi
    if [ "$i" -eq 10 ]; then
        echo "Health check failed; inspect: sudo journalctl -u goal-agent -n 100" >&2
        exit 1
    fi
    sleep 1
done

echo "=== Done ==="
echo "Check status: sudo systemctl status goal-agent"
echo "View logs:   sudo journalctl -u goal-agent -f"
echo "Health check: curl http://localhost:8899/health"
