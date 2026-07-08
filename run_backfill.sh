#!/usr/bin/env bash
# One-shot: prompts for bot token + webhook URL (hidden), backfills, commits, pushes.
# Usage:  bash run_backfill.sh
set -euo pipefail

cd "$(dirname "$0")"

read -rsp "Discord bot token: " DISCORD_BOT_TOKEN; echo
read -rsp "Discord webhook URL: " DISCORD_WEBHOOK_URL; echo
export DISCORD_BOT_TOKEN DISCORD_WEBHOOK_URL

python3 -m pip install -q -r requirements.txt >/dev/null 2>&1 || true
python3 backfill_from_discord.py

if [ -n "$(git status --porcelain data/ 2>/dev/null)" ]; then
  git add data/
  git commit -m "chore: backfill historical headlines from Discord"
  git pull --rebase origin main
  git push
  echo "✅ backfilled + pushed."
else
  echo "ℹ️  no new records to commit."
fi
