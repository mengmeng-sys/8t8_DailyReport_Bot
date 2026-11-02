#!/usr/bin/env bash
# Run this ONCE, inside the bot folder:   bash install_autostart.sh
# After that the bot starts by itself when you log in to Ubuntu, and starts again if it stops.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$DIR/venv/bin/python"

[ -x "$PY" ] || { echo "I cannot find $PY"; echo "Make the venv first:  python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"; exit 1; }
[ -f "$DIR/.env" ] || { echo "I cannot find $DIR/.env (it needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)"; exit 1; }
"$PY" -c "import telegram, openpyxl, lxml, dotenv, httpx" 2>/dev/null || { echo "Some packages are missing. Run:  source venv/bin/activate && pip install -r requirements.txt"; exit 1; }

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/hostel-bot.service" <<UNIT
[Unit]
Description=Hostel report Telegram bot

[Service]
WorkingDirectory=$DIR
ExecStart="$PY" "$DIR/bot.py"
Restart=always
RestartSec=15
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now hostel-bot.service
sleep 3
systemctl --user --no-pager status hostel-bot.service | head -12 || true
echo
echo "Done. The bot now starts when you log in."
echo "To start it even BEFORE you log in (after a restart), run once:  sudo loginctl enable-linger $USER"
echo "See the log:   journalctl --user -u hostel-bot -f"
echo "Stop it:       systemctl --user disable --now hostel-bot"
