#!/usr/bin/env bash
#
# Налаштування Monobank webhook через nip.io + Caddy (без власного домену).
# Запускати на НОВОМУ VPS з-під користувача з sudo:
#   bash deploy/setup_webhook_nipio.sh
#
# Що робить:
#   1. визначає публічний IP сервера
#   2. формує nip.io-хост (напр. 203-0-113-45.nip.io)
#   3. ставить Caddy і пише Caddyfile (reverse_proxy → localhost:8080)
#   4. прописує MONO_WEBHOOK_URL у .env проєкту
#   5. перезапускає Caddy
# Далі лишається перезапустити бота: sudo systemctl restart plussender

set -euo pipefail

# ── Шлях до .env проєкту (за потреби змініть) ───────────────────────────────
PROJECT_DIR="${PROJECT_DIR:-$HOME/PlusSender}"
ENV_FILE="$PROJECT_DIR/.env"

# ── 1. Публічний IP ─────────────────────────────────────────────────────────
IP="$(curl -fsS https://api.ipify.org || curl -fsS https://ifconfig.me)"
if [[ -z "${IP:-}" ]]; then
  echo "❌ Не вдалося визначити публічний IP. Вкажіть вручну: IP=1.2.3.4 bash $0"
  exit 1
fi
echo "▶ Публічний IP: $IP"

# ── 2. nip.io-хост ──────────────────────────────────────────────────────────
HOST="$(echo "$IP" | tr '.' '-').nip.io"
WEBHOOK_URL="https://$HOST/mono-webhook"
echo "▶ Хост:    $HOST"
echo "▶ Webhook: $WEBHOOK_URL"

# ── 3. Caddy ────────────────────────────────────────────────────────────────
if ! command -v caddy >/dev/null 2>&1; then
  echo "▶ Встановлюю Caddy…"
  sudo apt update
  sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt update
  sudo apt install -y caddy
fi

echo "▶ Пишу /etc/caddy/Caddyfile…"
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$HOST {
    reverse_proxy localhost:8080
}
EOF

# ── 4. .env ─────────────────────────────────────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
  if grep -q '^MONO_WEBHOOK_URL=' "$ENV_FILE"; then
    sudo sed -i "s|^MONO_WEBHOOK_URL=.*|MONO_WEBHOOK_URL=$WEBHOOK_URL|" "$ENV_FILE"
  else
    echo "MONO_WEBHOOK_URL=$WEBHOOK_URL" | sudo tee -a "$ENV_FILE" >/dev/null
  fi
  echo "▶ MONO_WEBHOOK_URL прописано у $ENV_FILE"
else
  echo "⚠ $ENV_FILE не знайдено — пропишіть вручну: MONO_WEBHOOK_URL=$WEBHOOK_URL"
fi

# ── 5. Фаєрвол + рестарт Caddy ──────────────────────────────────────────────
if command -v ufw >/dev/null 2>&1; then
  sudo ufw allow 80/tcp  || true
  sudo ufw allow 443/tcp || true
fi

sudo systemctl enable caddy
sudo systemctl restart caddy
sleep 2

echo
# Webhook відповідає лише на шляху з секретом: /mono-webhook/<MONO_WEBHOOK_SECRET>
SECRET="$(grep -s '^MONO_WEBHOOK_SECRET=' "$ENV_FILE" | cut -d= -f2- || true)"
if [[ -n "$SECRET" ]]; then
  echo "✅ Caddy налаштовано. Перевірка (після рестарту бота має бути 200):"
  curl -sS -o /dev/null -w "%{http_code}\n" "https://$HOST/mono-webhook/$SECRET" || true
else
  echo "⚠ У $ENV_FILE немає MONO_WEBHOOK_SECRET — без нього webhook не запуститься."
  echo "  Згенеруйте: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
fi
echo
echo "Тепер перезапустіть бота, щоб він зареєстрував webhook:"
echo "   sudo systemctl restart plussender"
echo "   journalctl -u plussender | grep -i webhook"
