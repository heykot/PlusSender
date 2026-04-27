"""Майстер підключення Telegram-сесії для нового користувача.

UX-покращення відносно старої версії:
  • api_id та api_hash можна ввести разом (через пробіл / двокрапку / новий рядок)
  • Якщо вже є збережена сесія — пропонуємо лишити або замінити
  • Код приймається в будь-якому форматі (з пробілами, тире, дужками)
  • Зрозумілі повідомлення про помилки на кожному кроці
  • Після успіху — прямий перехід у налаштування розсилки
  • Активний Telethon-клієнт зберігається в FSM, без глобальних словників
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from ...config import (
    BRAND,
    BTN_CANCEL,
    BTN_CONNECT,
    DIV,
    EMO,
)
from ...storage import (
    load_user,
    save_user,
    session_file_path,
    session_path,
)
from ...utils import card, h, section, step_indicator
from ..keyboards import (
    cancel_kb,
    connect_existing_session_kb,
    connect_intro_kb,
    connect_post_success_kb,
    main_menu_kb,
)
from ..states import ConnectStates

log = logging.getLogger(__name__)
router = Router(name="connect")

# Активні Telethon-клієнти на час wizard'у. Поза майстром — None.
# Ключ: user_id, значення: TelegramClient.
_active_clients: dict[int, TelegramClient] = {}


# ===================== Утиліти =====================
async def _disconnect_active(user_id: int) -> None:
    client = _active_clients.pop(user_id, None)
    if not client:
        return
    try:
        await client.disconnect()
    except Exception:
        pass


def _parse_credentials(raw: str) -> Optional[tuple[int, str]]:
    """Парсить рядок з api_id та api_hash.

    Підтримувані формати:
      "12345678 abcdef..."
      "12345678:abcdef..."
      "api_id=12345678 api_hash=abcdef..."
      "12345678\nabcdef..."
    """
    # витягуємо числа і hex-рядки
    digits = re.findall(r"\d{5,12}", raw)
    hashes = re.findall(r"[A-Fa-f0-9]{30,40}", raw)
    if digits and hashes:
        try:
            return int(digits[0]), hashes[0]
        except ValueError:
            return None
    return None


def _normalize_phone(raw: str) -> Optional[str]:
    cleaned = re.sub(r"[\s\-()]", "", raw or "")
    if not cleaned:
        return None
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    if not re.fullmatch(r"\+\d{8,15}", cleaned):
        return None
    return cleaned


def _normalize_code(raw: str) -> str:
    """Прибирає пробіли, тире, дужки — лишає тільки цифри."""
    return re.sub(r"\D", "", raw or "")


# ===================== Точка входу =====================
@router.message(Command("connect"))
@router.message(F.text == BTN_CONNECT)
async def start_connection(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    sess_file = session_file_path(msg.from_user)
    has_session = os.path.isfile(sess_file)

    sess_name = msg.from_user.username or msg.from_user.id

    intro = card(
        title="Підключення Telegram-акаунта",
        emoji=EMO["key"],
        sections=[
            (
                "Що це таке",
                "Бот використовуватиме <b>вашу сесію</b> Telethon, щоб "
                "надсилати повідомлення від вашого імені у вибрані чати "
                "на тривогу та відбій.",
            ),
            (
                "Що знадобиться",
                "①  <b>api_id</b> та <b>api_hash</b>\n"
                "    <a href='https://my.telegram.org/auth'>my.telegram.org</a> → "
                "<i>API Development Tools</i> → створіть додаток\n"
                "②  Номер телефону <code>+380…</code>\n"
                "③  Код підтвердження з Telegram\n"
                "④  Пароль 2FA (якщо є)",
            ),
            (
                f"{EMO['shield']} Безпека",
                f"api_id/hash зберігаються лише у вашому профілі.\n"
                f"Сесія Telethon — файл <code>sessions/{sess_name}.session</code>.",
            ),
        ],
    )
    await msg.answer(intro, disable_web_page_preview=True)

    if has_session:
        await msg.answer(
            f"{EMO['info']}  <b>У вас уже є збережена сесія</b>\n"
            f"<i>Що робимо?</i>",
            reply_markup=connect_existing_session_kb(),
        )
        return

    await msg.answer(
        f"{EMO['rocket']}  <b>Готові почати?</b>\n"
        f"<i>Натискайте кнопку нижче — і поїхали.</i>",
        reply_markup=connect_intro_kb(),
    )


# ===================== Існуюча сесія =====================
@router.callback_query(F.data == "connect:keep")
async def keep_existing(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Сесію збережено")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(
        f"{EMO['ok']} Працюємо з поточною сесією.",
        reply_markup=main_menu_kb(call.from_user),
    )


@router.callback_query(F.data == "connect:replace")
async def replace_existing(call: types.CallbackQuery, state: FSMContext) -> None:
    sess = session_file_path(call.from_user)
    try:
        if os.path.isfile(sess):
            os.remove(sess)
    except Exception as e:
        log.warning("Не вдалося видалити стару сесію: %s", e)
    await call.answer("Стару сесію видалено")
    await call.message.edit_reply_markup(reply_markup=None)
    await _begin_credentials(call.message, state, call.from_user)


@router.callback_query(F.data == "connect:cancel")
async def cancel_intro(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Скасовано")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("❎ Скасовано.", reply_markup=main_menu_kb(call.from_user))


@router.callback_query(F.data == "connect:start")
async def begin_via_button(call: types.CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await _begin_credentials(call.message, state, call.from_user)


async def _begin_credentials(msg: types.Message, state: FSMContext, user: types.User) -> None:
    await state.set_state(ConnectStates.waiting_credentials)
    text = (
        f"{EMO['key']}  <b>API credentials</b>\n"
        f"{step_indicator(1, 4)}\n"
        f"{DIV}\n"
        f"Надішліть <b>api_id</b> та <b>api_hash</b> <u>одним повідомленням</u>.\n\n"
        f"<b>Приклади:</b>\n"
        f"<code>12345678 abcdef0123456789abcdef0123456789</code>\n"
        f"<code>12345678:abcdef0123456789abcdef0123456789</code>"
    )
    await msg.answer(text, reply_markup=cancel_kb())


# ===================== Крок 1: api credentials =====================
@router.message(ConnectStates.waiting_credentials)
async def step_credentials(msg: types.Message, state: FSMContext) -> None:
    parsed = _parse_credentials(msg.text or "")
    if not parsed:
        await msg.answer(
            f"{EMO['err']}  <b>Не зміг розпізнати credentials</b>\n\n"
            f"<i>Підказки:</i>\n"
            f"  • <b>api_id</b> — число (зазвичай 7–8 цифр)\n"
            f"  • <b>api_hash</b> — рядок із 32 hex-символів\n\n"
            f"Спробуйте ще раз або натисніть «{h(BTN_CANCEL)}»."
        )
        return

    api_id, api_hash = parsed

    # Зберігаємо в профіль одразу
    data = load_user(msg.from_user)
    data.update(
        {
            "user_id": msg.from_user.id,
            "user_name": msg.from_user.username,
            "api_id": api_id,
            "api_hash": api_hash,
        }
    )
    save_user(msg.from_user, data)
    await state.update_data(api_id=api_id, api_hash=api_hash)
    await state.set_state(ConnectStates.waiting_phone)

    await msg.answer(
        f"{EMO['ok']}  Прийнято: <code>api_id={api_id}</code>\n\n"
        f"{EMO['phone']}  <b>Номер телефону</b>\n"
        f"{step_indicator(2, 4)}\n"
        f"{DIV}\n"
        f"Надішліть номер у форматі <code>+380XXXXXXXXX</code>:",
        reply_markup=cancel_kb(),
    )


# ===================== Крок 2: телефон =====================
@router.message(ConnectStates.waiting_phone)
async def step_phone(msg: types.Message, state: FSMContext) -> None:
    phone = _normalize_phone(msg.text or "")
    if not phone:
        await msg.answer(
            f"{EMO['err']}  <b>Невірний формат номера</b>\n"
            f"<i>Приклад:</i> <code>+380501234567</code>"
        )
        return

    fsm_data = await state.get_data()
    api_id = int(fsm_data["api_id"])
    api_hash = str(fsm_data["api_hash"])

    # Підключаємось і просимо код
    client = TelegramClient(session_path(msg.from_user), api_id, api_hash)
    try:
        await client.connect()
    except Exception as e:
        await msg.answer(
            f"{EMO['err']} Не вдалося з'єднатися з Telegram: {h(str(e))}"
        )
        try:
            await client.disconnect()
        except Exception:
            pass
        return

    try:
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await msg.answer(f"{EMO['err']} Номер недійсний. Перевірте і надішліть ще раз.")
        try:
            await client.disconnect()
        except Exception:
            pass
        return
    except Exception as e:
        await msg.answer(f"{EMO['err']} Помилка надсилання коду: {h(str(e))}")
        try:
            await client.disconnect()
        except Exception:
            pass
        return

    _active_clients[msg.from_user.id] = client
    await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
    await state.set_state(ConnectStates.waiting_code)

    await msg.answer(
        f"{EMO['code']}  <b>Код підтвердження</b>\n"
        f"{step_indicator(3, 4)}\n"
        f"{DIV}\n"
        f"Telegram надіслав код у ваш акаунт. Введіть його сюди.\n\n"
        f"<i>Формат не важливий — приймаю в будь-якому вигляді:</i>\n"
        f"  <code>1 2 3 4 5</code>   <code>1-2-3-4-5</code>   <code>12345</code>\n\n"
        f"{EMO['warn']}  <b>Не пересилайте код!</b>\n"
        f"<i>Введіть його вручну, інакше Telegram заблокує авторизацію.</i>",
        reply_markup=cancel_kb(),
    )


# ===================== Крок 3: код =====================
@router.message(ConnectStates.waiting_code)
async def step_code(msg: types.Message, state: FSMContext) -> None:
    client = _active_clients.get(msg.from_user.id)
    if not client:
        await state.clear()
        await msg.answer(
            f"{EMO['warn']} Сесію перервано. Почніть заново через «{h(BTN_CONNECT)}».",
            reply_markup=main_menu_kb(msg.from_user),
        )
        return

    code = _normalize_code(msg.text or "")
    if not code:
        await msg.answer(
            f"{EMO['err']}  <b>У повідомленні немає цифр</b>\n"
            f"<i>Введіть код з Telegram.</i>"
        )
        return

    fsm_data = await state.get_data()
    phone = fsm_data.get("phone")
    code_hash = fsm_data.get("phone_code_hash")

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
    except SessionPasswordNeededError:
        await state.set_state(ConnectStates.waiting_password)
        await msg.answer(
            f"{EMO['lock']}  <b>Двофакторна автентифікація</b>\n"
            f"{step_indicator(4, 4)}\n"
            f"{DIV}\n"
            f"На вашому акаунті ввімкнено 2FA.\n"
            f"<i>Введіть cloud password:</i>",
            reply_markup=cancel_kb(),
        )
        return
    except PhoneCodeInvalidError:
        await msg.answer(
            f"{EMO['err']}  <b>Невірний код</b>\n"
            f"<i>Спробуйте ще раз.</i>"
        )
        return
    except PhoneCodeExpiredError:
        await msg.answer(
            f"{EMO['err']}  <b>Код прострочений</b>\n"
            f"<i>Почніть підключення заново через «{h(BTN_CONNECT)}».</i>"
        )
        await _disconnect_active(msg.from_user.id)
        await state.clear()
        return
    except Exception as e:
        await msg.answer(
            f"{EMO['err']}  <b>Помилка авторизації</b>\n<code>{h(str(e))}</code>"
        )
        return

    await _finish_success(msg, state)


# ===================== Крок 4: 2FA =====================
@router.message(ConnectStates.waiting_password)
async def step_password(msg: types.Message, state: FSMContext) -> None:
    client = _active_clients.get(msg.from_user.id)
    if not client:
        await state.clear()
        await msg.answer(
            f"{EMO['warn']} Сесію перервано. Почніть заново.",
            reply_markup=main_menu_kb(msg.from_user),
        )
        return

    password = (msg.text or "").strip()
    if not password:
        await msg.answer(f"{EMO['err']} Пароль порожній. Введіть пароль 2FA:")
        return

    try:
        await client.sign_in(password=password)
    except Exception as e:
        await msg.answer(f"{EMO['err']} Невірний пароль: {h(str(e))}")
        return

    await _finish_success(msg, state)


# ===================== Завершення =====================
async def _finish_success(msg: types.Message, state: FSMContext) -> None:
    await _disconnect_active(msg.from_user.id)
    await state.clear()

    success_text = (
        f"{EMO['ok']} <b>Сесію успішно створено!</b>\n{HR}\n"
        f"Тепер бот зможе надсилати повідомлення від вашого імені на тривогу/відбій у Києві.\n\n"
        f"📋 <b>Що далі:</b>\n"
        f"1️⃣  Натисніть «🎯 Налаштувати розсилку» — оберіть чати, тексти й затримки\n"
        f"2️⃣  Поверніться у меню та натисніть «▶️ Старт» щоб увімкнути авто-розсилку\n"
        f"3️⃣  Стан можна перевірити в «👤 Профіль»\n\n"
        f"{EMO['warn']} Для початку роботи потрібен активний доступ — перевірте в розділі «💳 Оплата»."
    )
    await msg.answer(success_text, reply_markup=main_menu_kb(msg.from_user))
    await msg.answer(
        f"{EMO['bolt']} Зробимо налаштування зараз?",
        reply_markup=connect_post_success_kb(),
    )


# ===================== Швидкі переходи після успіху =====================
@router.callback_query(F.data == "connect:open_broadcast")
async def open_broadcast_after(call: types.CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # делегуємо обробнику в broadcast.py — імпорт у функції, щоб уникнути цикл-імпорту
    from .broadcast import show_broadcast_settings

    fake_msg = call.message
    fake_msg.from_user = call.from_user  # type: ignore[attr-defined]
    await show_broadcast_settings(fake_msg, state=state)


@router.callback_query(F.data == "connect:open_profile")
async def open_profile_after(call: types.CallbackQuery) -> None:
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    from .profile import show_profile

    fake_msg = call.message
    fake_msg.from_user = call.from_user  # type: ignore[attr-defined]
    await show_profile(fake_msg)
