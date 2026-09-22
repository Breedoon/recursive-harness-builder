#!/bin/bash
set -euo pipefail
cd /workspace/obs
set -a
. /workspace/runtime/state/telegram-media-bot/bot.env
set +a
exec /workspace/obs/.venv/bin/python -m obs_agent.telegram_media_bot
