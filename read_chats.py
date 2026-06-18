#!/usr/bin/env python3
"""
read_chats.py — окрема утиліта для перегляду переписок із збережених сесій.

Сам знаходить профілі в  user_data/*.json  і відповідні  sessions/<ім'я>.session,
під'єднується через Telethon (connect() + перевірка авторизації, БЕЗ start()),
показує список чатів і дає прочитати історію повідомлень будь-якого чату.

Запуск (з venv проєкту, де вже стоїть telethon):
    ./venv/bin/python read_chats.py                  # інтерактивний режим
    ./venv/bin/python read_chats.py --user hey_kot   # одразу обрати профіль
    ./venv/bin/python read_chats.py --user hey_kot --chat -1001234567890 --limit 100
    ./venv/bin/python read_chats.py --user hey_kot --chat "назва" --limit 50 --export out.txt

Нічого не змінює в сесіях і профілях — лише читає.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from telethon import TelegramClient, utils as tl_utils
except ImportError:
    sys.exit(
        "❌ Не знайдено telethon. Запускайте через venv проєкту:\n"
        "   ./venv/bin/python read_chats.py"
    )

PROJECT_ROOT = Path(__file__).resolve().parent
SESSIONS_DIR = PROJECT_ROOT / "sessions"
USERS_DIR = PROJECT_ROOT / "user_data"


# ───────────────────────── Проксі (опціонально, з .env) ─────────────────────────
def _load_proxy() -> Optional[tuple]:
    """Читає TELEGRAM_PROXY із .env, якщо є. Формат: socks5://host:port або http://host:port."""
    raw = os.getenv("TELEGRAM_PROXY", "").strip()
    if not raw:
        env_file = PROJECT_ROOT / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("TELEGRAM_PROXY="):
                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not raw:
        return None
    try:
        import socks  # noqa: F401  (python-socks / PySocks)
    except ImportError:
        print("⚠️  TELEGRAM_PROXY заданий, але немає PySocks — ігнорую проксі.")
        return None
    try:
        scheme, rest = raw.split("://", 1)
        host, port = rest.split(":", 1)
        proxy_type = {"socks5": 2, "socks4": 1, "http": 3}.get(scheme.lower(), 2)
        return (proxy_type, host, int(port))
    except Exception:
        print(f"⚠️  Не зміг розібрати TELEGRAM_PROXY='{raw}' — ігнорую.")
        return None


# ───────────────────────── Профілі ─────────────────────────
def discover_profiles() -> list[dict]:
    """Повертає список профілів: {key, user_id, username, api_id, api_hash, session, has_session}."""
    profiles: list[dict] = []
    if not USERS_DIR.is_dir():
        return profiles
    for jf in sorted(USERS_DIR.glob("*.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not (data.get("api_id") and data.get("api_hash")):
            continue
        key = jf.stem  # ім'я файлу = ім'я сесії (safe_username_from)
        session = SESSIONS_DIR / f"{key}.session"
        profiles.append({
            "key": key,
            "user_id": data.get("user_id"),
            "username": data.get("username") or data.get("user_name") or key,
            "api_id": int(data["api_id"]),
            "api_hash": str(data["api_hash"]),
            "session": str(SESSIONS_DIR / key),  # telethon додасть .session сам
            "has_session": session.is_file(),
            "status": data.get("status"),
            "access_until": data.get("access_until"),
        })
    return profiles


def pick_profile(profiles: list[dict], wanted: Optional[str]) -> Optional[dict]:
    if wanted:
        for p in profiles:
            if wanted in (p["key"], p["username"], str(p["user_id"])):
                return p
        print(f"❌ Профіль '{wanted}' не знайдено.")
        return None

    print("\n📂  Доступні профілі:")
    for i, p in enumerate(profiles, 1):
        sess = "✅" if p["has_session"] else "❌ немає .session"
        st = "🟢" if p.get("status") else "🔴"
        print(f"  [{i}]  {st} @{p['username']}  (id={p['user_id']})  "
              f"доступ до {p.get('access_until') or '—'}  {sess}")
    raw = input("\nОберіть номер профілю (Enter — вихід): ").strip()
    if not raw:
        return None
    try:
        idx = int(raw) - 1
        return profiles[idx]
    except (ValueError, IndexError):
        print("❌ Невірний вибір.")
        return None


# ───────────────────────── Робота з Telethon ─────────────────────────
async def list_dialogs(client: TelegramClient, limit: int = 50) -> list[dict]:
    items: list[dict] = []
    async for d in client.iter_dialogs(limit=limit):
        ent = d.entity
        items.append({
            "title": d.name or "—",
            "pid": tl_utils.get_peer_id(ent),
            "username": getattr(ent, "username", None),
            "kind": ent.__class__.__name__,
            "unread": getattr(d, "unread_count", 0),
        })
    return items


async def resolve_entity(client: TelegramClient, chat: str):
    """Розв'язує чат за числовим ID або частиною назви / @username."""
    # числовий ID?
    s = chat.strip()
    if s.lstrip("-").isdigit():
        pid = int(s)
        try:
            real_id, peer_cls = tl_utils.resolve_id(pid)
            return await client.get_input_entity(peer_cls(real_id))
        except Exception:
            return await client.get_entity(pid)
    # @username?
    if s.startswith("@"):
        return await client.get_entity(s)
    # пошук по назві серед діалогів
    q = s.lower()
    async for d in client.iter_dialogs(limit=None):
        if q in (d.name or "").lower():
            return d.entity
    raise ValueError(f"Чат '{chat}' не знайдено серед діалогів.")


async def read_history(client: TelegramClient, entity, limit: int) -> list[str]:
    lines: list[str] = []
    sender_cache: dict[int, str] = {}
    msgs = []
    async for m in client.iter_messages(entity, limit=limit):
        msgs.append(m)
    for m in reversed(msgs):  # від старих до нових
        when = m.date.astimezone().strftime("%Y-%m-%d %H:%M") if m.date else "—"
        sid = m.sender_id
        name = sender_cache.get(sid) if sid is not None else None
        if name is None:
            try:
                snd = await m.get_sender()
                name = (
                    getattr(snd, "title", None)
                    or " ".join(filter(None, [getattr(snd, "first_name", None),
                                              getattr(snd, "last_name", None)]))
                    or (f"@{snd.username}" if getattr(snd, "username", None) else None)
                    or (str(sid) if sid is not None else "—")
                )
            except Exception:
                name = str(sid) if sid is not None else "—"
            if sid is not None:
                sender_cache[sid] = name

        if m.text:
            body = m.text
        elif m.media:
            body = f"[медіа: {m.media.__class__.__name__}]"
        else:
            body = "[порожнє/службове]"
        lines.append(f"[{when}]  {name}:\n    {body}")
    return lines


async def interactive_chats(client: TelegramClient, args) -> None:
    # Прямий чат із аргументів
    if args.chat:
        entity = await resolve_entity(client, args.chat)
        lines = await read_history(client, entity, args.limit)
        output("\n".join(lines), args.export)
        return

    # Інтерактивний список діалогів
    dialogs = await list_dialogs(client, limit=args.dialogs)
    if not dialogs:
        print("⚠️  Діалогів не знайдено.")
        return
    print(f"\n💬  Останні чати (до {args.dialogs}):")
    for i, it in enumerate(dialogs, 1):
        u = f" @{it['username']}" if it["username"] else ""
        unread = f"  🔵{it['unread']}" if it["unread"] else ""
        print(f"  [{i:>2}]  {it['title']}{u}   ({it['kind']}, id={it['pid']}){unread}")

    while True:
        raw = input("\nНомер чату для перегляду (q — вихід): ").strip()
        if raw.lower() in ("q", "", "вихід"):
            return
        try:
            it = dialogs[int(raw) - 1]
        except (ValueError, IndexError):
            print("❌ Невірний номер.")
            continue
        lim_raw = input(f"Скільки повідомлень показати? (Enter = {args.limit}): ").strip()
        limit = int(lim_raw) if lim_raw.isdigit() else args.limit
        entity = await resolve_entity(client, str(it["pid"]))
        lines = await read_history(client, entity, limit)
        header = f"\n===== {it['title']} (id={it['pid']}) — {len(lines)} повідомлень =====\n"
        output(header + "\n".join(lines), args.export)
        if args.export:
            return  # експортували — завершуємо


def output(text: str, export: Optional[str]) -> None:
    if export:
        Path(export).write_text(text, encoding="utf-8")
        print(f"💾  Збережено у {export}  ({len(text)} символів)")
    else:
        print(text)


# ───────────────────────── main ─────────────────────────
async def run(args) -> int:
    profiles = discover_profiles()
    if not profiles:
        print(f"❌ У {USERS_DIR} немає профілів з api_id/api_hash.")
        return 1

    prof = pick_profile(profiles, args.user)
    if not prof:
        return 0
    if not prof["has_session"]:
        print(f"❌ Немає файлу сесії: {prof['session']}.session — користувач не підключений.")
        return 1

    proxy = _load_proxy()
    client = TelegramClient(prof["session"], prof["api_id"], prof["api_hash"], proxy=proxy)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print(f"❌ Сесія @{prof['username']} не авторизована. "
                  f"Користувач має заново пройти «🔌 Підключення» в боті.")
            return 1
        me = await client.get_me()
        who = f"@{me.username}" if me and me.username else (me.first_name if me else "?")
        print(f"\n✅ Підключено як {who}  (профіль @{prof['username']})")
        await interactive_chats(client, args)
        return 0
    except Exception as e:
        print(f"❌ Помилка: {type(e).__name__}: {e}")
        return 1
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Перегляд переписок зі збережених Telethon-сесій.")
    ap.add_argument("--user", help="ключ профілю / username / user_id (інакше — оберете зі списку)")
    ap.add_argument("--chat", help="ID, @username або частина назви чату (інакше — оберете зі списку)")
    ap.add_argument("--limit", type=int, default=50, help="скільки повідомлень читати (default 50)")
    ap.add_argument("--dialogs", type=int, default=40, help="скільки чатів показати у списку (default 40)")
    ap.add_argument("--export", help="зберегти результат у файл замість виводу в консоль")
    args = ap.parse_args()

    import asyncio
    try:
        rc = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nПерервано.")
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
