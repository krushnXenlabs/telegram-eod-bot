#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Install the EOD bot as a systemd service so it:
#   - Starts automatically when the VM boots
#   - Restarts if it ever crashes
#
# Run from ~/eod-bot AFTER filling in your .env:
#   chmod +x install_service.sh && ./install_service.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
USER="$(whoami)"

echo "Installing systemd service for EOD bot…"
echo "  Directory : $BOT_DIR"
echo "  User      : $USER"

sudo tee /etc/systemd/system/eod-bot.service > /dev/null <<EOF
[Unit]
Description=Telegram EOD Bot (Claude-powered)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${BOT_DIR}
EnvironmentFile=${BOT_DIR}/.env
ExecStart=${BOT_DIR}/venv/bin/python ${BOT_DIR}/eod_bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable eod-bot
sudo systemctl start eod-bot

echo ""
echo "✅ Service installed and started!"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status eod-bot    # check status"
echo "  sudo journalctl -u eod-bot -f    # live logs"
echo "  sudo systemctl restart eod-bot   # restart after changes"
echo "  sudo systemctl stop eod-bot      # stop the bot"
