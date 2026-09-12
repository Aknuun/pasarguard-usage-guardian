#!/usr/bin/env bash
# PasarGuard Usage Guardian installer
# Usage:  bash install.sh
set -euo pipefail

RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; DIM=$'\e[2m'; N=$'\e[0m'
info(){ echo "${DIM}$*${N}"; }
ok(){ echo "${GRN}✔ $*${N}"; }
warn(){ echo "${YLW}! $*${N}"; }
err(){ echo "${RED}✘ $*${N}" >&2; }

need_root(){ [[ $EUID -eq 0 ]] || { err "Run as root (sudo bash install.sh)"; exit 1; }; }

echo "==============================================="
echo "   PasarGuard Usage Guardian (نگهبان مصرف)"
echo "==============================================="

# ---- 1) Telegram bot token + admin chat id ----
while true; do
  read -rp "Bot token (from @BotFather): " BOT_TOKEN
  BOT_TOKEN="$(echo "$BOT_TOKEN" | tr -d '[:space:]')"
  [[ -n "$BOT_TOKEN" ]] || { warn "Token is required."; continue; }
  resp="$(curl -fsS --max-time 20 "https://api.telegram.org/bot${BOT_TOKEN}/getMe" 2>/dev/null || true)"
  if echo "$resp" | grep -q '"ok":true'; then ok "Token verified."; break; else warn "Invalid token, try again."; fi
done

while true; do
  read -rp "Admin numeric chat id (from @userinfobot): " CHAT_ID
  CHAT_ID="$(echo "$CHAT_ID" | tr -d '[:space:]')"
  [[ "$CHAT_ID" =~ ^-?[0-9]+$ ]] && break || warn "Enter a numeric id."
done

# ---- 2) Install dir ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${PG_INSTALL_DIR:-$SCRIPT_DIR}"
info "Install directory: $DIR"
mkdir -p "$DIR"

# ---- 3) Dependencies ----
if command -v apt-get >/dev/null 2>&1; then
  need_root
  apt-get update -qq >/dev/null 2>&1 || true
  apt-get install -y -qq python3 python3-pip curl cron >/dev/null 2>&1 || true
fi
python3 -m pip install --quiet --break-system-packages -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null \
  || python3 -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt"
ok "Python dependencies installed."

# ---- 4) Copy source into install dir (if different) ----
for f in monitor.py settings_bot.py requirements.txt; do
  [[ "$SCRIPT_DIR/$f" == "$DIR/$f" ]] || cp "$SCRIPT_DIR/$f" "$DIR/$f"
done
mkdir -p "$DIR/systemd"
cp "$SCRIPT_DIR/systemd/"*.service "$DIR/systemd/" 2>/dev/null || true
[[ -f "$DIR/config.json" ]] || cp "$SCRIPT_DIR/config.example.json" "$DIR/config.json" 2>/dev/null || true

# ---- 5) .env (bot token + chat id only; panel creds are asked in the bot) ----
cat > "$DIR/.env" <<EOF
PG_BOT_TOKEN=${BOT_TOKEN}
PG_CHAT_ID=${CHAT_ID}
EOF
chmod 600 "$DIR/.env"
ok "Wrote $DIR/.env"

# ---- 6) systemd service for the settings bot ----
SERVICE=/etc/systemd/system/pasarguard-usage-guardian-settings.service
sed "s#/root/config-monitor#${DIR}#g" "$SCRIPT_DIR/systemd/pasarguard-usage-guardian-settings.service" > "$SERVICE" 2>/dev/null || cat > "$SERVICE" <<EOF
[Unit]
Description=PasarGuard Usage Guardian - Telegram settings bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${DIR}
EnvironmentFile=-${DIR}/.env
ExecStart=/usr/bin/python3 ${DIR}/settings_bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now pasarguard-usage-guardian-settings.service
ok "Settings bot service running."

# ---- 7) hourly monitor via cron ----
CRON_LINE="30 * * * * /usr/bin/python3 ${DIR}/monitor.py hourly >> ${DIR}/cron.log 2>&1"
( crontab -l 2>/dev/null | grep -v "config-monitor/monitor.py\\|pasarguard-usage-guardian\|${DIR}/monitor.py" ; echo "$CRON_LINE" ) | crontab -
ok "Hourly monitor cron installed."

echo
ok "Installation finished."
echo "  • Open the bot in Telegram and send /start"
echo "  • The bot will ask for your Pasarguard panel address, username and password."
