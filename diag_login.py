"""
Діагностичний скрипт для перевірки Telegram-логіну через Telethon.

Запуск:
    cd ~/PlusSender
    source venv/bin/activate
    python diag_login.py

Що робить:
  1. Підбирає api_id/api_hash з вашого існуючого профілю (user_data/*.json)
     або просить ввести вручну.
  2. Запитує номер телефону.
  3. Викликає send_code_request і показує ДЕТАЛЬНО, що відповів Telegram.
  4. Якщо треба — приймає від вас код і завершує авторизацію.

Цей скрипт НЕ зачіпає ваш бот і не псує його сесії — використовує
окремий файл "sessions/_diag.session", який можна видалити після перевірки.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import sys
from pathlib import Path

try:
    from telethon import TelegramClient
    from telethon.errors import (
        ApiIdInvalidError,
        FloodWaitError,
        PhoneCodeExpiredError,
        PhoneCodeInvalidError,
        PhoneNumberBannedError,
        PhoneNumberFloodError,
        PhoneNumberInvalidError,
        SessionPasswordNeededError,
    )
except ImportError:
    print("❌ Telethon не встановлено. Активуйте venv:")
    print("   source venv/bin/activate")
    sys.exit(1)


ROOT = Path(__file__).resolve().parent
USER_DATA_DIR = ROOT / "user_data"
SESSIONS_DIR = ROOT / "sessions"
DIAG_SESSION = SESSIONS_DIR / "_diag"   # окремий файл, щоб не псувати робочі сесії


# ─────────────────────── Утиліти ───────────────────────

def _print_header(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def _ask(prompt: str, default: str | None = None) -> str:
    """Запитує введення з підказкою про дефолт."""
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or (default or "")


def _list_profiles() -> list[dict]:
    """Повертає список профілів з api_id/api_hash з user_data/."""
    out: list[dict] = []
    if not USER_DATA_DIR.is_dir():
        return out
    for path in sorted(glob.glob(str(USER_DATA_DIR / "*.json"))):
        try:
            data = json.load(open(path, "r", encoding="utf-8"))
        except Exception:
            continue
        if data.get("api_id") and data.get("api_hash"):
            out.append({
                "file": Path(path).name,
                "username": Path(path).stem,
                "user_id": data.get("user_id"),
                "api_id": data["api_id"],
                "api_hash": data["api_hash"],
            })
    return out


def _pick_credentials() -> tuple[int, str]:
    """Дає вибрати профіль або ввести ключі вручну."""
    profiles = _list_profiles()
    if profiles:
        _print_header("Знайдені профілі в user_data/")
        for i, p in enumerate(profiles, 1):
            print(f"  {i})  {p['file']}  →  api_id={p['api_id']}  (user_id={p['user_id']})")
        print(f"  0)  Ввести інші ключі вручну")
        choice = _ask("\nВаш вибір (номер)", "1")
        if choice and choice != "0":
            try:
                p = profiles[int(choice) - 1]
                return int(p["api_id"]), str(p["api_hash"])
            except (IndexError, ValueError):
                print("⚠️  Невірний вибір, переходжу до ручного вводу.")

    _print_header("Ручний ввід ключів")
    api_id = _ask("api_id (число)")
    api_hash = _ask("api_hash (32 символи)")
    return int(api_id), api_hash


def _describe_sent(sent) -> None:
    """Виводить деталізацію відповіді send_code_request."""
    type_name = type(sent.type).__name__ if sent.type else "—"
    next_name = type(sent.next_type).__name__ if sent.next_type else "—"
    timeout = getattr(sent, "timeout", "—")

    print()
    print(f"  📨  Тип надісланого коду:  {type_name}")
    print(f"  ➡️   Наступний дозволений:  {next_name}")
    print(f"  ⏱   Таймаут до повтору:    {timeout}")
    print()

    hints = {
        "SentCodeTypeApp":
            "→ Код надіслано В ЗАСТОСУНОК TELEGRAM.\n"
            "  Шукайте чат «Telegram» (синя галочка, аватар з літачком, ID 777000).\n"
            "  Має прийти на ВСІ ваші активні Telegram-сесії.",
        "SentCodeTypeSms":
            "→ Код надіслано SMS-кою на ваш номер.\n"
            "  Перевіряйте повідомлення на телефоні. Іноді приходить 30-60 сек.",
        "SentCodeTypeCall":
            "→ Telegram дзвонить голосом і диктує код. Підніміть слухавку.",
        "SentCodeTypeFlashCall":
            "→ Telegram робить короткий дзвінок. Код = останні цифри номера, що показався.",
        "SentCodeTypeMissedCall":
            "→ Telegram робить пропущений дзвінок. Код = останні цифри номера.",
        "SentCodeTypeEmailCode":
            "→ Код надіслано на ваш Telegram-email.",
    }
    if type_name in hints:
        print("  " + hints[type_name].replace("\n", "\n  "))
    print()


# ─────────────────────── Основна логіка ───────────────────────

async def main() -> None:
    _print_header("🔬  Діагностика Telegram-логіну")
    print(
        "Цей скрипт перевірить чи правильні ваші api_id/api_hash і чи\n"
        "доставляє Telegram код підтвердження для вашого номера.\n"
    )

    # 1. Ключі
    api_id, api_hash = _pick_credentials()

    # 2. Телефон
    _print_header("Номер телефону")
    phone = _ask("Введіть номер з кодом країни (+380XXXXXXXXX)").replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = "+" + phone

    # 3. Окрема сесія для діагностики
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    # Чистимо старі діагностичні файли — щоб був повністю свіжий старт
    for ext in ("", "-journal", "-wal", "-shm"):
        f = DIAG_SESSION.with_suffix(f".session{ext}")
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass

    _print_header("Підключення до Telegram")
    print(f"  api_id:   {api_id}")
    print(f"  api_hash: {api_hash[:6]}…{api_hash[-4:]}")
    print(f"  phone:    {phone}")
    print(f"  session:  {DIAG_SESSION}.session")
    print()

    client = TelegramClient(str(DIAG_SESSION), api_id, api_hash)

    try:
        print("  • Підключаюсь до серверів Telegram...")
        await client.connect()
        print("  ✅ З'єднання встановлено.")
        print()

        # 4. Запит коду
        print("  • Викликаю send_code_request()...")
        try:
            sent = await client.send_code_request(phone)
        except ApiIdInvalidError:
            print()
            print("  ❌ ПОМИЛКА: ApiIdInvalidError")
            print("     Telegram каже що ваші api_id/api_hash недійсні.")
            print("     Лікування: створіть НОВУ пару на my.telegram.org → API Development Tools.")
            return
        except PhoneNumberInvalidError:
            print()
            print(f"  ❌ ПОМИЛКА: PhoneNumberInvalidError")
            print(f"     Telegram не приймає номер {phone}.")
            print(f"     Перевірте формат і чи зареєстрований цей номер у Telegram.")
            return
        except PhoneNumberBannedError:
            print()
            print(f"  ❌ ПОМИЛКА: PhoneNumberBannedError")
            print(f"     Номер {phone} забанено Telegram.")
            print(f"     Підтримка: https://telegram.org/support")
            return
        except PhoneNumberFloodError:
            print()
            print(f"  ❌ ПОМИЛКА: PhoneNumberFloodError")
            print(f"     Забагато запитів для цього номера за останній час.")
            print(f"     Зачекайте 1-3 години і спробуйте ще раз — БЕЗ повторних спроб.")
            return
        except FloodWaitError as e:
            print()
            print(f"  ❌ ПОМИЛКА: FloodWaitError — Telegram сказав почекати {e.seconds} сек")
            print(f"     Це ~{e.seconds // 60} хвилин. До цього часу нічого не пробуйте.")
            return
        except Exception as e:
            print()
            print(f"  ❌ НЕОЧІКУВАНА ПОМИЛКА: {type(e).__name__}")
            print(f"     {e}")
            return

        print("  ✅ Запит на код прийнято Telegram'ом.")
        _describe_sent(sent)

        # 5. Чекаємо код
        _print_header("Введіть код, який прийшов")
        print(
            "  Введіть код з Telegram (можна з пробілами і тире).\n"
            "  Або натисніть Enter, щоб лише перевірити отримання запиту без логіну.\n"
        )
        code_raw = input("Код: ").strip()
        if not code_raw:
            print()
            print("  ℹ️  Логін пропущено — діагностика завершена.")
            return

        code = "".join(c for c in code_raw if c.isdigit())
        if not code:
            print("  ⚠️  У вашому вводі немає цифр. Виходжу.")
            return

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except PhoneCodeInvalidError:
            print()
            print("  ❌ Код неправильний. Введіть свіжий код і запустіть скрипт знов.")
            return
        except PhoneCodeExpiredError:
            print()
            print("  ❌ Код прострочений. Запустіть скрипт ще раз — буде новий код.")
            return
        except SessionPasswordNeededError:
            print()
            print("  ℹ️  Увімкнено 2FA. Введіть ваш cloud password:")
            pw = input("Пароль: ").strip()
            if not pw:
                print("  ⚠️  Порожній пароль. Виходжу.")
                return
            try:
                await client.sign_in(password=pw)
            except Exception as e:
                print(f"  ❌ Пароль не підійшов: {e}")
                return

        # 6. Успіх — показуємо хто залогінився
        me = await client.get_me()
        _print_header("✅  ЛОГІН УСПІШНИЙ!")
        print(f"  Залогінений як:  {me.first_name or ''} {me.last_name or ''}  @{me.username or '—'}")
        print(f"  User ID:         {me.id}")
        print(f"  Phone:           {me.phone}")
        print()
        print(f"  Це підтверджує що api_id/api_hash + цей номер РОБОЧІ.")
        print(f"  Якщо у боті всеодно не виходить — киньте сюди логи з journalctl.")

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        print()
        print(f"ℹ️  Діагностична сесія залишилась у файлі: {DIAG_SESSION}.session")
        print(f"   Можна видалити: rm -f {DIAG_SESSION}.session*")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nСкасовано.")
        sys.exit(130)
