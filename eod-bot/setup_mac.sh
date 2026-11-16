#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# EOD Bot — Mac Setup Script
# Run from the eod-bot folder:
#   chmod +x setup_mac.sh && ./setup_mac.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  EOD Bot — Mac Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 1. Check Python
echo "[1/3] Checking Python…"
if ! command -v python3 &>/dev/null; then
    echo "❌ Python3 not found. Install it from https://python.org or via: brew install python"
    exit 1
fi
echo "✅ $(python3 --version)"

# 2. Create virtual environment and install deps
echo "[2/3] Creating virtual environment and installing dependencies…"
python3 -m venv venv
source venv/bin/activate
pip install --quiet --upgrade pip
pip install -r requirements.txt
deactivate

# 3. Create .env from example if it doesn't exist
echo "[3/3] Setting up .env file…"
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ✅ Setup complete!"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  Next: fill in your credentials:"
    echo "    nano .env"
    echo ""
    echo "  Then start the bot:"
    echo "    source venv/bin/activate && python eod_bot.py"
    echo ""
    echo "  (Send /start to your bot in Telegram to get your User ID)"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
else
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ✅ Setup complete! (.env already exists)"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  Start the bot:"
    echo "    source venv/bin/activate && python eod_bot.py"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
fi
