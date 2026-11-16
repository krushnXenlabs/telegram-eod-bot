#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — Push latest bot code to your GCS VM and restart the service
#
# Usage (run from your Mac, inside the eod-bot folder):
#   ./deploy.sh
#
# Prerequisites:
#   - gcloud CLI installed on your Mac  (brew install google-cloud-sdk)
#   - VM_NAME / VM_ZONE set below, or exported as env vars before running
# ─────────────────────────────────────────────────────────────────────────────

set -e

# ── Edit these two lines to match your VM ────────────────────────────────────
VM_NAME="${VM_NAME:-eod-bot-vm}"
VM_ZONE="${VM_ZONE:-us-central1-a}"
# ─────────────────────────────────────────────────────────────────────────────

REMOTE_DIR="~/eod-bot"
FILES="eod_bot.py requirements.txt personality.md"

echo "Deploying to $VM_NAME ($VM_ZONE)..."

# Copy changed files to the VM
gcloud compute scp $FILES "$VM_NAME:$REMOTE_DIR/" --zone="$VM_ZONE"

# Restart the service to pick up changes
gcloud compute ssh "$VM_NAME" --zone="$VM_ZONE" --command="
  cd $REMOTE_DIR
  source venv/bin/activate
  pip install --quiet -r requirements.txt
  deactivate
  sudo systemctl restart eod-bot
  sudo systemctl status eod-bot --no-pager
"

echo ""
echo "Done! Bot is restarted with latest code."
echo "To tail live logs: gcloud compute ssh $VM_NAME --zone=$VM_ZONE --command='sudo journalctl -u eod-bot -f'"
