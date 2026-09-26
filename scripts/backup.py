#!/usr/bin/env python3
"""Щоденний бекап даних бота: профілі, сесії, адміни, журнал оплат, .env.

Сесії Telethon — це SQLite, тому копіюємо їх через sqlite3 backup API
(консистентна копія навіть поки бот працює), решту — як є.

Запуск (cron, щодня о 04:30):
    30 4 * * * cd ~/PlusSender && ./venv/bin/python scripts/backup.py >> logs/backup.log 2>&1

Зберігає останні KEEP архівів у ~/backups/PlusSender-auto-*.tgz
(ручні бекапи з іншою назвою не чіпає).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = Path(os.environ.get("BACKUP_DIR", Path.home() / "backups"))
KEEP = int(os.environ.get("BACKUP_KEEP", "14"))
FILES = ["admins.json", "processed_payments.json", ".env", "logs/payments.log"]


def main() -> int:
    os.umask(0o077)  # у бекапі сесії та секрети
    DEST.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = DEST / f"PlusSender-auto-{stamp}.tgz"

    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "PlusSender"
        stage.mkdir()
        if (ROOT / "user_data").is_dir():
            shutil.copytree(ROOT / "user_data", stage / "user_data")
        sessions = stage / "sessions"
        sessions.mkdir()
        n_sessions = 0
        for src in (ROOT / "sessions").glob("*.session"):
            with sqlite3.connect(src) as s, sqlite3.connect(sessions / src.name) as d:
                s.backup(d)
            n_sessions += 1
        for rel in FILES:
            if (ROOT / rel).is_file():
                (stage / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / rel, stage / rel)
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(stage, arcname="PlusSender")

    old = sorted(DEST.glob("PlusSender-auto-*.tgz"))[:-KEEP]
    for p in old:
        p.unlink()
    n_users = len(list((ROOT / "user_data").glob("*.json")))
    print(f"{stamp} backup ok: {archive.name} ({archive.stat().st_size // 1024} KB, "
          f"{n_users} профілів, {n_sessions} сесій), видалено старих: {len(old)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
