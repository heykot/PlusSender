"""Генерація Telethon-сесії через QR-код на основі профілів у user_data/.

Що робить:
  • читає api_id / api_hash із JSON-файлів у user_data/
  • показує QR-код прямо в терміналі (скануєш телефоном того акаунта)
  • створює файл сесії у sessions/<ім'я профілю>.session
  • підтримує 2FA (хмарний пароль)

Запуск:
  python qr_session.py                # покаже список профілів, обираєш номер
  python qr_session.py hey_kot        # одразу для конкретного профілю
  python qr_session.py 621739213      # або за user_id

Ім'я файлу сесії збігається з ім'ям JSON-файлу профілю — саме так,
як очікує бот (storage.session_path).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

PROJECT_ROOT = Path(__file__).resolve().parent
USERS_DIR = PROJECT_ROOT / "user_data"
SESSIONS_DIR = PROJECT_ROOT / "sessions"

QR_TOKEN_WAIT = 25          # скільки чекати один токен (сек) перед оновленням
QR_TOTAL_TIMEOUT = 300      # загальний ліміт очікування сканування (сек)


# ----------------------- читання профілів -----------------------
def _load_profiles() -> list[dict]:
    """Повертає список профілів: [{name, api_id, api_hash, user_id}, ...]."""
    profiles: list[dict] = []
    if not USERS_DIR.is_dir():
        return profiles
    for path in sorted(USERS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        api_id = data.get("api_id")
        api_hash = data.get("api_hash")
        if not api_id or not api_hash:
            continue
        profiles.append({
            "name": path.stem,                 # ← ім'я сесії = ім'я файлу профілю
            "api_id": int(api_id),
            "api_hash": str(api_hash),
            "user_id": data.get("user_id"),
            "user_name": data.get("user_name") or data.get("username") or path.stem,
        })
    return profiles


def _pick_profile(profiles: list[dict], arg: str | None) -> dict | None:
    if not profiles:
        print("❌  У user_data/ немає профілів з api_id/api_hash.")
        return None

    # Якщо передано аргумент — шукаємо за ім'ям / user_id
    if arg:
        for p in profiles:
            if arg == p["name"] or arg == str(p.get("user_id")):
                return p
        print(f"❌  Профіль «{arg}» не знайдено.")
        return None

    # Інтерактивний вибір
    print("\nДоступні профілі:")
    for i, p in enumerate(profiles, 1):
        has = (SESSIONS_DIR / f"{p['name']}.session").is_file()
        mark = "✅ є сесія" if has else "—"
        print(f"  {i}. {p['name']:<20} api_id={p['api_id']:<12} {mark}")
    print()
    try:
        choice = input("Введіть номер профілю (Enter — перший): ").strip()
    except EOFError:
        choice = ""
    if not choice:
        return profiles[0]
    if choice.isdigit() and 1 <= int(choice) <= len(profiles):
        return profiles[int(choice) - 1]
    # можливо ввели ім'я
    for p in profiles:
        if choice == p["name"] or choice == str(p.get("user_id")):
            return p
    print("❌  Невірний вибір.")
    return None


# ----------------------- друк QR у терміналі -----------------------
def _print_qr(url: str) -> None:
    try:
        import segno
        print()
        segno.make(url, error="m").terminal(compact=True)
        print()
    except Exception:
        # segno немає — даємо посилання, його можна вставити у генератор QR
        print("\n(segno не встановлено — встановіть: pip install segno)")
        print("Посилання для QR:\n", url, "\n")


# ----------------------- основна логіка -----------------------
async def generate(profile: dict) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session_path = str(SESSIONS_DIR / profile["name"])

    print(f"\n▶  Профіль: {profile['name']}  (api_id={profile['api_id']})")
    print(f"   Сесія буде збережена у: sessions/{profile['name']}.session")

    client = TelegramClient(session_path, profile["api_id"], profile["api_hash"])
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"\n✅  Сесія вже авторизована: {me.username or me.id}. Нічого робити не треба.")
        await client.disconnect()
        return

    qr = await client.qr_login()
    print("\n📱  Відскануйте QR акаунтом, для якого робите сесію:")
    print("    Telegram → Налаштування → Пристрої → Підключити пристрій")
    _print_qr(qr.url)

    loop = asyncio.get_event_loop()
    deadline = loop.time() + QR_TOTAL_TIMEOUT

    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                print("\n⌛  Час вийшов — QR ніхто не відсканував. Запустіть скрипт ще раз.")
                await client.disconnect()
                return
            try:
                await qr.wait(timeout=min(QR_TOKEN_WAIT, remaining))
            except asyncio.TimeoutError:
                # токен застарів — оновлюємо і перемальовуємо
                try:
                    await qr.recreate()
                except Exception:
                    continue
                print("🔄  Оновлюю QR-код (старий протермінувався)…")
                _print_qr(qr.url)
                continue
            except SessionPasswordNeededError:
                await _ask_password(client)
                break
            else:
                break

        me = await client.get_me()
        print(f"\n✅  Готово! Сесію створено: sessions/{profile['name']}.session")
        print(f"   Акаунт: {me.first_name or ''} @{me.username or me.id}")
    finally:
        await client.disconnect()


async def _ask_password(client: TelegramClient) -> None:
    """2FA: просимо хмарний пароль (кілька спроб)."""
    import getpass

    for _ in range(3):
        try:
            pw = getpass.getpass("\n🔐  Введіть хмарний пароль 2FA: ")
        except EOFError:
            pw = input("\n🔐  Введіть хмарний пароль 2FA: ")
        try:
            await client.sign_in(password=pw)
            return
        except Exception as e:
            print(f"   ❌  Пароль не підійшов: {e}")
    raise SystemExit("Не вдалося ввести правильний пароль 2FA.")


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    profiles = _load_profiles()
    profile = _pick_profile(profiles, arg)
    if not profile:
        sys.exit(1)
    asyncio.run(generate(profile))


if __name__ == "__main__":
    main()
