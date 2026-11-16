#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# EOD Bot — Google Cloud VM Setup Script
# Run this ONCE after SSH-ing into your VM for the first time:
#   chmod +x setup.sh && ./setup.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  EOD Bot — Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 1. Update system
echo "[1/6] Updating system packages…"
sudo apt-get update -qq && sudo apt-get upgrade -y -qq

# 2. Install Python 3.11+ and pip
echo "[2/6] Installing Python…"
sudo apt-get install -y -qq python3 python3-pip python3-venv

# 3. Create project directory
echo "[3/6] Creating project directory at ~/eod-bot …"
mkdir -p ~/eod-bot
cp eod_bot.py requirements.txt .env.example ~/eod-bot/
cd ~/eod-bot

# 4. Create virtualenv and install deps
echo "[4/6] Creating Python virtual environment and installing dependencies…"
python3 -m venv venv
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
deactivate

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  [5/6] Configure your credentials"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Copy the example env file and fill it in:"
echo ""
echo "    cd ~/eod-bot"
echo "    cp .env.example .env"
echo "    nano .env          # fill in your 3 values"
echo ""
echo "  Then install the systemd service to keep it running:"
echo ""
echo "    ./install_service.sh"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
