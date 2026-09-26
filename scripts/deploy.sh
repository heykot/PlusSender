#!/usr/bin/env bash
# Деплой на сервер через git: запускати ЛОКАЛЬНО після `git push`.
#   bash scripts/deploy.sh
# Сервер підтягує origin/main (лише fast-forward), оновлює залежності,
# перевіряє, що код компілюється, і перезапускає бота.
set -euo pipefail

HOST="${DEPLOY_HOST:-ubuntu@89.168.113.153}"
KEY="${DEPLOY_KEY:-$HOME/.ssh/plussender}"

ssh -i "$KEY" "$HOST" 'set -euo pipefail
cd ~/PlusSender
git fetch --quiet origin
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "❌ На сервері є незакомічені зміни у відстежуваних файлах — деплой зупинено:"
  git status --short
  exit 1
fi
git pull --ff-only --quiet origin main
./venv/bin/pip install -q -r requirements.txt
./venv/bin/python -m compileall -q plus_sender
sudo systemctl restart plussender
sleep 10
systemctl is-active plussender
echo "✅ Задеплоєно: $(git log -1 --format="%h %s")"
tail -n 3 logs/bot.log'
