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
    DIV_THIN,
    EMO,
)
from ...storage import (
    load_user,
    save_user,
    session_file_path,
    session_path,
)
from ...utils import (
    big_step_header,
    card,
    example_block,
    h,
    next_hint,
    section,
    soft_error,
    step_indicator,
    tip,
)
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
        title="Підключення вашого Telegram",
        emoji=EMO["key"],
        sections=[
            (
                "Навіщо це потрібно",
                "Щоб бот міг надсилати повідомлення <b>від вашого імені</b> "
                "(а не «від бота»), йому потрібен доступ до вашого акаунта "
                "через офіційні ключі Telegram.\n"
                "<i>Це робиться один раз — далі все працює само.</i>",
            ),
            (
                "Що знадобиться (4 речі)",
                "①  🔑  <b>api_id</b> та <b>api_hash</b>\n"
                "      <i>Беруться на</i> "
                "<a href='https://my.telegram.org/auth'>my.telegram.org</a> "
                "<i>→ API Development Tools → Create application</i>\n\n"
                "②  📱  <b>Номер телефону</b> у форматі <code>+380XXXXXXXXX</code>\n\n"
                "③  🔢  <b>Код</b>, який Telegram надішле у ваш акаунт\n\n"
                "④  🔐  <b>Пароль 2FA</b> — лише якщо у вас увімкнена двофакторка",
            ),
            (
                f"{EMO['shield']} Це безпечно?",
                "<b>Так.</b> Ключі зберігаються тільки у вашому профілі тут, "
                "на сервері бота. Сесія — це файл "
                f"<code>sessions/{sess_name}.session</code>.\n"
                "Жодних паролів я не бачу й не передаю.",
            ),
        ],
    )
    await msg.answer(intro, disable_web_page_preview=True)

    if has_session:
        await msg.answer(
            f"{EMO['info']}  <b>У вас уже є збережена сесія</b>\n"
            f"<i>Можна продовжити з нею або створити нову.\n"
            f"Якщо все працює — лишайте поточну.</i>",
            reply_markup=connect_existing_session_kb(),
        )
        return

    await msg.answer(
        f"{EMO['rocket']}  <b>Готові почати?</b>\n"
        f"<i>Це займе 2–3 хвилини. Натискайте «🚀 Почати» — і поїхали!</i>",
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
        f"{big_step_header(1, 4, 'Ключі API', emoji=EMO['key'])}\n\n"
        f"Зайдіть на "
        f"<a href='https://my.telegram.org/auth'>my.telegram.org</a> → "
        f"<b>API Development Tools</b> → створіть додаток.\n"
        f"Скопіюйте <b>api_id</b> (число) та <b>api_hash</b> (довгий рядок) "
        f"і надішліть сюди <u>одним повідомленням</u>.\n\n"
        f"{example_block('12345678 abcdef0123456789abcdef0123456789', '12345678:abcdef0123456789abcdef0123456789')}\n\n"
        f"{tip('формат не важливий — пробіл, двокрапка, новий рядок чи навіть з підписами.')}"
    )
    await msg.answer(text, reply_markup=cancel_kb(), disable_web_page_preview=True)


# ===================== Крок 1: api credentials =====================
@router.message(ConnectStates.waiting_credentials)
async def step_credentials(msg: types.Message, state: FSMContext) -> None:
    parsed = _parse_credentials(msg.text or "")
    if not parsed:
        await msg.answer(
            soft_error(
                "Не зміг розпізнати ключі",
                body=(
                    "Перевірте, що ви скопіювали обидва значення:\n"
                    "  • <b>api_id</b> — число (зазвичай 7–8 цифр)\n"
                    "  • <b>api_hash</b> — рядок із 32 hex-символів\n\n"
                    + example_block(
                        "12345678 abcdef0123456789abcdef0123456789",
                        "12345678:abcdef0123456789abcdef0123456789",
                    )
                    + f"\n\nАбо натисніть «{h(BTN_CANCEL)}» щоб вийти."
                ),
                retry=False,
            )
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
        f"{EMO['ok']}  <b>Чудово!</b>  Ключі прийнято: "
        f"<code>api_id={api_id}</code>\n\n"
        f"{big_step_header(2, 4, 'Номер телефону', emoji=EMO['phone'])}\n\n"
        f"Надішліть номер вашого Telegram у міжнародному форматі.\n\n"
        f"{example_block('+380501234567', '+380 50 123 45 67')}\n\n"
        f"{tip('пробіли і дужки приберу автоматично — головне щоб був код країни.')}",
        reply_markup=cancel_kb(),
    )


# ===================== Крок 2: телефон =====================
@router.message(ConnectStates.waiting_phone)
async def step_phone(msg: types.Message, state: FSMContext) -> None:
    phone = _normalize_phone(msg.text or "")
    if not phone:
        await msg.answer(
            soft_error(
                "Не схоже на номер телефону",
                body=example_block("+380501234567", "+380 50 123 45 67"),
            )
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
            soft_error(
                "Не вдалося з'єднатися з Telegram",
                body=f"<code>{h(str(e))}</code>\n\n"
                     f"<i>Перевірте інтернет і повторіть «🔌 Підключити».</i>",
                retry=False,
            )
        )
        try:
            await client.disconnect()
        except Exception:
            pass
        return

    try:
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await msg.answer(
            soft_error(
                "Номер не приймається Telegram",
                body="Перевірте, чи правильно скопійовано номер. Має бути зареєстрований у Telegram.",
            )
        )
        try:
            await client.disconnect()
        except Exception:
            pass
        return
    except Exception as e:
        await msg.answer(
            soft_error(
                "Не вдалося надіслати код",
                body=f"<code>{h(str(e))}</code>",
                retry=False,
            )
        )
        try:
            await client.disconnect()
        except Exception:
            pass
        return

    _active_clients[msg.from_user.id] = client
    await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
    await state.set_state(ConnectStates.waiting_code)

    await msg.answer(
        f"{EMO['ok']}  <b>Номер прийнято.</b>  Telegram уже надіслав код у ваш акаунт.\n\n"
        f"{big_step_header(3, 4, 'Код підтвердження', emoji=EMO['code'])}\n\n"
        f"Введіть код, який ви щойно отримали від Telegram.\n\n"
        f"{example_block('1 2 3 4 5', '12345', '1-2-3-4-5')}\n\n"
        f"{tip('пробіли, дужки і тире — не проблема. Я витягну тільки цифри.')}",
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
            soft_error(
                "У повідомленні не знайшов жодної цифри",
                body="Введіть код, який Telegram надіслав вам у застосунок.",
            )
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
            f"{big_step_header(4, 4, 'Пароль 2FA (двофакторка)', emoji=EMO['lock'])}\n\n"
            f"На вашому акаунті ввімкнено двофакторну автентифікацію.\n"
            f"Введіть свій <b>cloud password</b> від Telegram <u>точно як є</u>, "
            f"одним повідомленням.\n\n"
            f"{tip('повідомлення з паролем буде <b>видалено одразу</b> після отримання — для безпеки.')}",
            reply_markup=cancel_kb(),
        )
        return
    except PhoneCodeInvalidError:
        await msg.answer(
            soft_error(
                "Код не підійшов",
                body="Перевірте, чи ви скопіювали останній код, який надіслав Telegram.",
            )
        )
        return
    except PhoneCodeExpiredError:
        await msg.answer(
            soft_error(
                "Код вже прострочений",
                body=f"Telegram дає на код кілька хвилин. Почніть заново через «{h(BTN_CONNECT)}» — "
                     f"ми надішлемо новий код.",
                retry=False,
            )
        )
        await _disconnect_active(msg.from_user.id)
        await state.clear()
        return
    except Exception as e:
        await msg.answer(
            soft_error(
                "Не вийшло авторизуватися",
                body=f"<code>{h(str(e))}</code>",
                retry=False,
            )
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

    password = msg.text or ""
    # Видаляємо повідомлення з паролем одразу для безпеки
    try:
        await msg.delete()
    except Exception:
        pass

    if not password:
        await msg.answer(
            soft_error(
                "Пароль порожній",
                body="Введіть свій cloud password від Telegram (двофакторна автентифікація).",
            )
        )
        return

    try:
        await client.sign_in(password=password)
    except Exception as e:
        await msg.answer(
            soft_error(
                "Пароль не підійшов",
                body=f"<code>{h(str(e))}</code>\n\n"
                     f"<i>Перевірте розкладку та регістр літер.</i>",
            )
        )
        return

    await _finish_success(msg, state)


# ===================== Завершення =====================
async def _finish_success(msg: types.Message, state: FSMContext) -> None:
    await _disconnect_active(msg.from_user.id)
    await state.clear()

    success_text = (
        f"🎉  <b>Готово! Сесію створено.</b>\n"
        f"{DIV}\n"
        f"Тепер бот зможе надсилати повідомлення <b>від вашого імені</b> "
        f"у вибрані чати на тривогу й відбій у Києві.\n\n"
        f"<b>Залишилось 2 кроки:</b>\n"
        f"  ①  🎛  <b>Налаштування</b>  →  оберіть чати та що в них надсилати\n"
        f"  ②  ▶️  <b>Старт</b>  →  увімкніть авто-роботу\n\n"
        f"{EMO['warn']}  <i>Якщо бот не реагує — перевірте, чи активний "
        f"доступ у «💳 Оплата».</i>"
    )
    await msg.answer(success_text, reply_markup=main_menu_kb(msg.from_user))
    await msg.answer(
        f"{EMO['bolt']}  <b>Хочете налаштувати розсилку зараз?</b>\n"
        f"<i>Це найцікавіша частина — обираємо чати та що надсилати.</i>",
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

    fake_msg = call.message.model_copy(update={"from_user": call.from_user})
    await show_broadcast_settings(fake_msg, state=state)


@router.callback_query(F.data == "connect:open_profile")
async def open_profile_after(call: types.CallbackQuery) -> None:
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    from .profile import show_profile

    fake_msg = call.message.model_copy(update={"from_user": call.from_user})
    await show_profile(fake_msg)
