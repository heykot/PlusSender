"""Майстер підключення Telegram-сесії для нового користувача.

Потік:
  1. Номер телефону — кнопкою «📱 Поділитися номером» (або текстом).
  2. Код — набирається на inline-клавіатурі, щоб не потрапити в чат
     повідомленням (інакше Telegram вважає код «пересланим» і блокує вхід).
  3. Пароль 2FA — лише якщо ввімкнено.

api_id/api_hash беруться зі спільних TG_API_ID/TG_API_HASH бота; якщо їх
не задано (або користувач обрав «Власні ключі») — спершу питаємо ключі.
Альтернатива коду — вхід через QR з другого пристрою.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
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
    BTN_CANCEL,
    BTN_CONNECT,
    BTN_CONNECT_QR,
    BTN_OWN_KEYS,
    DIV,
    EMO,
    SHARED_API_CREDENTIALS,
)
from ...storage import (
    load_user,
    save_user,
    session_file_path,
    session_path,
)
from ...utils import (
    big_step_header,
    example_block,
    h,
    soft_error,
    tip,
)
from ..keyboards import (
    cancel_kb,
    code_keypad_kb,
    connect_existing_session_kb,
    connect_phone_kb,
    connect_post_success_kb,
    connect_qr_kb,
    main_menu_kb,
)
from ..states import ConnectStates

log = logging.getLogger(__name__)
router = Router(name="connect")

# Активні Telethon-клієнти на час wizard'у. Поза майстром — None.
# Ключ: user_id, значення: TelegramClient.
_active_clients: dict[int, TelegramClient] = {}

# Активні QR-логіни та фонові задачі очікування сканування.
_qr_logins: dict[int, object] = {}        # user_id -> QRLogin
_qr_tasks: dict[int, asyncio.Task] = {}   # user_id -> очікувач

# aiogram обробляє апдейти паралельно. Кроки входу (натискання цифр, код,
# пароль) серіалізуємо по користувачу: інакше швидкі натискання губили б
# цифри, а повторно надісланий пароль ішов би в уже закритий клієнт.
_step_locks: dict[int, asyncio.Lock] = {}


def _step_lock(user_id: int) -> asyncio.Lock:
    return _step_locks.setdefault(user_id, asyncio.Lock())


async def _in_state(state: FSMContext, expected) -> bool:
    return await state.get_state() == expected.state

# Скільки всього чекаємо на сканування QR (сек), і час життя одного токена.
QR_TOTAL_TIMEOUT = 300
QR_TOKEN_WAIT = 25


# ===================== Утиліти =====================
async def _disconnect_active(user_id: int) -> None:
    # Спершу гасимо фоновий очікувач QR, якщо є
    task = _qr_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    _qr_logins.pop(user_id, None)

    client = _active_clients.pop(user_id, None)
    if not client:
        return
    try:
        await client.disconnect()
    except Exception:
        pass


def _qr_png_bytes(url: str) -> Optional[bytes]:
    """Генерує PNG з QR-кодом. None — якщо segno не встановлено."""
    try:
        import segno
    except Exception:
        return None
    try:
        buf = io.BytesIO()
        segno.make(url, error="m").save(buf, kind="png", scale=8, border=2)
        return buf.getvalue()
    except Exception:
        return None


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


def _sent_code_where(sent_obj) -> str:
    """Дружній опис того, КУДИ Telegram надіслав код."""
    type_name = type(sent_obj.type).__name__ if sent_obj.type else ""
    return {
        "SentCodeTypeApp":
            "📱 <b>у застосунок Telegram</b>\n"
            "   Шукайте чат <b>«Telegram»</b> (синя галочка, аватар з літачком).",
        "SentCodeTypeSms":
            "💬 <b>SMS-повідомленням</b> на ваш номер.\n"
            "   Може йти до хвилини.",
        "SentCodeTypeCall":
            "📞 <b>голосовим дзвінком</b> — підніміть слухавку, бот продиктує код.",
        "SentCodeTypeFlashCall":
            "📞 <b>коротким дзвінком</b> — введіть <b>останні цифри</b> номера, що подзвонив.",
        "SentCodeTypeMissedCall":
            "📞 <b>пропущеним дзвінком</b> — введіть <b>останні цифри</b> номера, що подзвонив.",
        "SentCodeTypeEmailCode":
            "✉️ <b>листом</b> на ваш Telegram-email.",
    }.get(type_name, "у Telegram (місце невідоме — перевірте додаток і SMS)")


@router.message(Command("connect"))
@router.message(F.text == BTN_CONNECT)
async def start_connection(msg: types.Message, state: FSMContext) -> None:
    await _disconnect_active(msg.from_user.id)
    await state.clear()

    if os.path.isfile(session_file_path(msg.from_user)):
        await msg.answer(
            f"{EMO['info']}  <b>Ваш Telegram уже підключено</b>\n"
            f"<i>Якщо розсилка працює — лишайте як є.</i>",
            reply_markup=connect_existing_session_kb(),
        )
        return

    await _begin_connect(msg, state)


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
    await _begin_connect(call.message, state)


@router.callback_query(F.data == "connect:cancel")
async def cancel_intro(call: types.CallbackQuery, state: FSMContext) -> None:
    # Якщо є активний Telethon-клієнт (з кроку телефону) — закриваємо
    await _disconnect_active(call.from_user.id)
    await state.clear()
    await call.answer("Скасовано")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.message.answer("❎ Скасовано.", reply_markup=main_menu_kb(call.from_user))


# ===================== Початок =====================
async def _begin_connect(msg: types.Message, state: FSMContext) -> None:
    """Зі спільними ключами бота — одразу крок телефону, інакше — ключі."""
    if SHARED_API_CREDENTIALS:
        api_id, api_hash = SHARED_API_CREDENTIALS
        await state.update_data(api_id=api_id, api_hash=api_hash)
        await _ask_phone(msg, state)
    else:
        await _ask_credentials(msg, state)


@router.callback_query(F.data == "connect:own_keys")
async def own_keys(call: types.CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await _disconnect_active(call.from_user.id)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _ask_credentials(call.message, state)


async def _ask_phone(msg: types.Message, state: FSMContext) -> None:
    await state.set_state(ConnectStates.waiting_phone)
    await msg.answer(
        f"{EMO['key']}  <b>Підключення вашого Telegram</b>\n"
        f"{DIV}\n"
        f"Щоб надсилати повідомлення <b>від вашого імені</b>, боту потрібен "
        f"вхід у ваш акаунт — як на новому пристрої. Це робиться один раз.\n\n"
        f"{big_step_header(1, 2, 'Номер телефону', emoji=EMO['phone'])}\n\n"
        f"Натисніть <b>«📱 Поділитися номером»</b> внизу 👇 — або введіть номер "
        f"у форматі <code>+380XXXXXXXXX</code>.\n\n"
        f"<i>Сесія зберігається лише на сервері бота. Завершити її можна будь-коли: "
        f"Telegram → Налаштування → Пристрої.</i>",
        reply_markup=connect_phone_kb(own_keys=bool(SHARED_API_CREDENTIALS)),
    )


async def _ask_credentials(msg: types.Message, state: FSMContext) -> None:
    await state.set_state(ConnectStates.waiting_credentials)
    text = (
        f"{EMO['key']}  <b>Ключі API</b>\n\n"
        f"Зайдіть на <b>my.telegram.org</b> → "
        f"<b>API Development Tools</b> → створіть додаток.\n"
        f"Скопіюйте <b>api_id</b> (число) та <b>api_hash</b> (довгий рядок) "
        f"і надішліть сюди <u>одним повідомленням</u>.\n\n"
        f"{example_block('12345678 abcdef0123456789abcdef0123456789', '12345678:abcdef0123456789abcdef0123456789')}\n\n"
        f"{tip('формат не важливий — пробіл, двокрапка, новий рядок чи навіть з підписами.')}"
    )

    # Inline-кнопка: швидкий перехід на my.telegram.org
    open_kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text="🌐  Відкрити my.telegram.org",
            url="https://my.telegram.org/auth",
        )],
    ])
    await msg.answer(text, reply_markup=open_kb, disable_web_page_preview=True)
    await msg.answer(
        "<i>👆 Натисніть кнопку щоб відкрити сайт, або вставте ключі сюди.</i>",
        reply_markup=cancel_kb(),
    )


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
    await state.update_data(api_id=api_id, api_hash=api_hash)
    await msg.answer(f"{EMO['ok']}  Ключі прийнято: <code>api_id={api_id}</code>")
    await _ask_phone(msg, state)


# ===================== Вибір способу входу =====================
_QR_CAPTION = (
    "🔳  <b>Вхід через QR-код</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "Відскануйте цей QR з <b>іншого пристрою</b>, де ви вже зайшли в Telegram:\n"
    "Telegram → <b>Налаштування → Пристрої → Підключити пристрій</b> → "
    "наведіть камеру на цей QR.\n\n"
    "⚠️  <b>З одного телефону QR відсканувати не можна.</b>\n"
    "Якщо у вас лише цей телефон — натисніть «↩️ Скасувати» і оберіть "
    "<b>«🔢 Код / SMS»</b>: код прийде у ваш Telegram (чат «Telegram»).\n\n"
    "<i>Код діє близько хвилини й оновлюється сам, поки ви не підтвердите.</i>"
)

_QR_2FA_PROMPT = (
    "🔐  <b>Потрібен пароль 2FA</b>\n"
    "На вашому акаунті ввімкнено двофакторну автентифікацію.\n"
    "Введіть свій <b>cloud password</b> від Telegram <u>точно як є</u>, "
    "одним повідомленням.\n\n"
    "<i>Повідомлення з паролем буде видалено одразу після отримання.</i>"
)


@router.callback_query(F.data == "connect:method_qr")
async def method_qr(call: types.CallbackQuery, state: FSMContext) -> None:
    await call.answer("Готую QR-код…")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _start_qr_login(call.message, state, call.from_user)


async def _start_qr_login(msg: types.Message, state: FSMContext, user: types.User) -> None:
    fsm_data = await state.get_data()
    try:
        api_id = int(fsm_data["api_id"])
        api_hash = str(fsm_data["api_hash"])
    except (KeyError, ValueError, TypeError):
        await msg.answer(
            soft_error(
                "Загубилися ключі API",
                body=f"Почніть заново через «{h(BTN_CONNECT)}».",
                retry=False,
            )
        )
        await state.clear()
        return

    # Закриваємо попередній клієнт, якщо лишився
    await _disconnect_active(user.id)

    client = TelegramClient(session_path(user), api_id, api_hash)
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

    # Якщо сесія раптом уже авторизована — нічого питати не треба
    try:
        if await client.is_user_authorized():
            _active_clients[user.id] = client
            await _finish_success_core(msg.bot, msg.chat.id, user, state)
            return
    except Exception:
        pass

    try:
        qr = await client.qr_login()
    except SessionPasswordNeededError:
        _active_clients[user.id] = client
        await state.set_state(ConnectStates.waiting_password)
        await msg.answer(_QR_2FA_PROMPT, reply_markup=cancel_kb())
        return
    except Exception as e:
        await msg.answer(
            soft_error(
                "Не вдалося створити QR-код",
                body=f"<code>{h(str(e))}</code>",
                retry=False,
            )
        )
        try:
            await client.disconnect()
        except Exception:
            pass
        return

    _active_clients[user.id] = client
    _qr_logins[user.id] = qr
    await state.set_state(ConnectStates.waiting_qr)

    png = _qr_png_bytes(qr.url)
    kb = connect_qr_kb(qr.url)
    if png:
        sent = await msg.answer_photo(
            types.BufferedInputFile(png, filename="login_qr.png"),
            caption=_QR_CAPTION,
            reply_markup=kb,
        )
    else:
        # segno не встановлено — даємо тільки кнопку-тап (цього достатньо на телефоні)
        sent = await msg.answer(
            _QR_CAPTION + "\n\n<i>(QR-картинку не згенеровано — користуйтесь кнопкою нижче.)</i>",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    await msg.answer(
        "<i>👆 Очікую підтвердження входу…</i>",
        reply_markup=cancel_kb(),
    )

    task = asyncio.create_task(
        _qr_waiter(msg.bot, sent.chat.id, sent.message_id, user, state, bool(png))
    )
    _qr_tasks[user.id] = task


async def _refresh_qr_message(
    bot, chat_id: int, message_id: int, url: str, is_photo: bool
) -> None:
    """Оновлює повідомлення з QR після перевипуску токена."""
    kb = connect_qr_kb(url)
    if is_photo:
        png = _qr_png_bytes(url)
        if png:
            try:
                await bot.edit_message_media(
                    media=types.InputMediaPhoto(
                        media=types.BufferedInputFile(png, filename="login_qr.png"),
                        caption=_QR_CAPTION,
                    ),
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=kb,
                )
                return
            except Exception:
                pass
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=kb
        )
    except Exception:
        pass


async def _qr_waiter(
    bot, chat_id: int, message_id: int, user: types.User, state: FSMContext, is_photo: bool
) -> None:
    """Фоново чекає сканування QR, оновлюючи токен, поки не сплине ліміт."""
    user_id = user.id
    qr = _qr_logins.get(user_id)
    if qr is None:
        return
    deadline = time.monotonic() + QR_TOTAL_TIMEOUT
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await bot.send_message(
                    chat_id,
                    "⌛  <b>Час очікування вийшов.</b>\n"
                    f"<i>Спробуйте ще раз через «{h(BTN_CONNECT)}».</i>",
                    reply_markup=main_menu_kb(user),
                )
                await _disconnect_active(user_id)
                await state.clear()
                return

            try:
                await qr.wait(timeout=min(QR_TOKEN_WAIT, remaining))
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                # Токен застарів — перевипускаємо й оновлюємо повідомлення
                try:
                    await qr.recreate()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
                await _refresh_qr_message(bot, chat_id, message_id, qr.url, is_photo)
                continue
            except SessionPasswordNeededError:
                await state.set_state(ConnectStates.waiting_password)
                await bot.send_message(chat_id, _QR_2FA_PROMPT, reply_markup=cancel_kb())
                _qr_tasks.pop(user_id, None)
                _qr_logins.pop(user_id, None)
                return
            except Exception as e:
                log.warning("connect: qr wait failed: %s", e)
                await bot.send_message(
                    chat_id,
                    soft_error(
                        "Не вдалося завершити вхід по QR",
                        body=f"<code>{h(str(e))}</code>",
                        retry=False,
                    ),
                )
                await _disconnect_active(user_id)
                await state.clear()
                return
            else:
                # Успішно авторизовано
                _qr_tasks.pop(user_id, None)
                _qr_logins.pop(user_id, None)
                await _finish_success_core(bot, chat_id, user, state)
                return
    except asyncio.CancelledError:
        # Скасування — клієнт закриє ініціатор (_disconnect_active)
        pass


# ===================== Крок 1: телефон =====================
@router.message(ConnectStates.waiting_phone, F.text == BTN_CONNECT_QR)
async def phone_step_qr(msg: types.Message, state: FSMContext) -> None:
    await _start_qr_login(msg, state, msg.from_user)


@router.message(ConnectStates.waiting_phone, F.text == BTN_OWN_KEYS)
async def phone_step_own_keys(msg: types.Message, state: FSMContext) -> None:
    await _ask_credentials(msg, state)


@router.message(ConnectStates.waiting_phone, F.contact)
async def step_phone_contact(msg: types.Message, state: FSMContext) -> None:
    contact = msg.contact
    # Кнопка request_contact завжди шле власний номер; чужий контакт (переслана
    # візитка) має інший user_id — такий не приймаємо.
    if contact.user_id != msg.from_user.id:
        await msg.answer(
            soft_error(
                "Це не ваш номер",
                body="Натисніть кнопку «📱 Поділитися номером» внизу — Telegram надішле саме ваш номер.",
            )
        )
        return
    phone = _normalize_phone(contact.phone_number)
    if not phone:
        await msg.answer(soft_error("Не вдалося прочитати номер", body="Введіть його вручну: <code>+380XXXXXXXXX</code>"))
        return
    await _request_code(msg, state, phone)


@router.message(ConnectStates.waiting_phone)
async def step_phone(msg: types.Message, state: FSMContext) -> None:
    phone = _normalize_phone(msg.text or "")
    if not phone:
        await msg.answer(
            soft_error(
                "Не схоже на номер телефону",
                body="Натисніть «📱 Поділитися номером» внизу або введіть номер так:\n"
                     + example_block("+380501234567", "+380 50 123 45 67"),
            )
        )
        return
    await _request_code(msg, state, phone)


async def _request_code(msg: types.Message, state: FSMContext, phone: str) -> None:
    fsm_data = await state.get_data()
    try:
        api_id = int(fsm_data["api_id"])
        api_hash = str(fsm_data["api_hash"])
    except (KeyError, ValueError, TypeError):
        await state.clear()
        await msg.answer(
            soft_error("Загубилися дані підключення", body=f"Почніть заново через «{h(BTN_CONNECT)}».", retry=False),
            reply_markup=main_menu_kb(msg.from_user),
        )
        return

    # Якщо перед цим користувач пробував QR — закриваємо той клієнт
    await _disconnect_active(msg.from_user.id)

    client = TelegramClient(session_path(msg.from_user), api_id, api_hash)
    try:
        await client.connect()
    except Exception as e:
        await msg.answer(
            soft_error(
                "Не вдалося з'єднатися з Telegram",
                body=f"<code>{h(str(e))}</code>\n\n"
                     f"<i>Спробуйте ще раз через хвилину.</i>",
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
                body="Перевірте номер. Він має бути зареєстрований у Telegram.",
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
    await state.update_data(phone=phone)
    await state.set_state(ConnectStates.waiting_code)

    log.info(
        "connect: code requested phone=%s type=%s next=%s timeout=%s",
        phone,
        type(sent.type).__name__ if sent.type else "?",
        type(sent.next_type).__name__ if sent.next_type else "—",
        getattr(sent, "timeout", "?"),
    )

    # Reply-клавіатуру з кнопкою номера міняємо на «Скасувати»
    await msg.answer(f"{EMO['ok']}  <b>Номер прийнято.</b>", reply_markup=cancel_kb())
    await _send_code_keypad(msg.bot, msg.chat.id, state, sent)


# ===================== Крок 2: код (inline-клавіатура) =====================
_CODE_MAX_LEN = 10


def _code_prompt_text(where: str, entered: str, length: int, note: str = "") -> str:
    slots = list(entered)
    if length > len(slots):
        slots += ["_"] * (length - len(slots))
    display = " ".join(slots) if slots else "—"
    text = (
        f"{big_step_header(2, 2, 'Код підтвердження', emoji=EMO['code'])}\n\n"
        f"📨  <b>Куди надіслано код:</b>\n   {where}\n\n"
        f"Наберіть код <b>кнопками нижче</b> 👇\n"
        f"<i>Не надсилайте його повідомленням — Telegram вважатиме код "
        f"пересланим і заблокує вхід.</i>\n\n"
        f"<b>Код:</b>  <code>{display}</code>"
    )
    return f"{text}\n\n{note}" if note else text


async def _send_code_keypad(bot, chat_id: int, state: FSMContext, sent) -> None:
    where = _sent_code_where(sent)
    length = int(getattr(sent.type, "length", 0) or 0)
    can_resend = sent.next_type is not None
    prompt = await bot.send_message(
        chat_id,
        _code_prompt_text(where, "", length),
        reply_markup=code_keypad_kb(can_resend),
        disable_web_page_preview=True,
    )
    await state.update_data(
        phone_code_hash=sent.phone_code_hash,
        code_input="",
        code_len=length,
        code_where=where,
        code_can_resend=can_resend,
        code_msg_id=prompt.message_id,
    )


async def _update_code_prompt(
    bot, chat_id: int, fsm: dict, entered: str, note: str = "", keypad: bool = True
) -> None:
    msg_id = fsm.get("code_msg_id")
    if not msg_id:
        return
    try:
        await bot.edit_message_text(
            _code_prompt_text(str(fsm.get("code_where") or ""), entered, int(fsm.get("code_len") or 0), note),
            chat_id=chat_id,
            message_id=msg_id,
            reply_markup=code_keypad_kb(bool(fsm.get("code_can_resend"))) if keypad else None,
            disable_web_page_preview=True,
        )
    except Exception:
        pass  # «message is not modified» тощо


@router.callback_query(F.data.startswith("code:"), ConnectStates.waiting_code)
async def code_keypad(call: types.CallbackQuery, state: FSMContext) -> None:
    async with _step_lock(call.from_user.id):
        # Поки чекали на лок, код міг бути вже прийнятий
        if not await _in_state(state, ConnectStates.waiting_code):
            await call.answer()
            return
        await _code_keypad_step(call, state)


async def _code_keypad_step(call: types.CallbackQuery, state: FSMContext) -> None:
    fsm = await state.get_data()
    entered = str(fsm.get("code_input") or "")
    length = int(fsm.get("code_len") or 0)

    if call.data.startswith("code:d:"):
        if len(entered) < (length or _CODE_MAX_LEN):
            entered += call.data[-1]
    elif call.data == "code:back":
        entered = entered[:-1]

    submit = call.data == "code:ok" or (length and len(entered) == length)
    if submit and not entered:
        await call.answer("Спершу наберіть код")
        return
    await call.answer()
    await state.update_data(code_input=entered)

    if not submit:
        await _update_code_prompt(call.bot, call.message.chat.id, fsm, entered)
        return

    await _update_code_prompt(call.bot, call.message.chat.id, fsm, entered, note="⏳ <i>Перевіряю…</i>", keypad=False)
    await _submit_code(call.bot, call.message.chat.id, call.from_user, state, entered)


@router.callback_query(F.data.startswith("code:"))
async def code_keypad_stale(call: types.CallbackQuery) -> None:
    await call.answer(f"Цей вхід уже завершено. Почніть заново через «{BTN_CONNECT}».", show_alert=True)


@router.message(ConnectStates.waiting_code)
async def step_code_text(msg: types.Message, state: FSMContext) -> None:
    """Запасний шлях: код надіслали текстом. Часто це вже заблокований код,
    тому видаляємо повідомлення й підказуємо про клавіатуру."""
    code = _normalize_code(msg.text or "")
    if not code:
        await msg.answer(
            soft_error(
                "Наберіть код кнопками",
                body="Використовуйте цифрову клавіатуру під повідомленням з кодом 👆",
            )
        )
        return
    try:
        await msg.delete()
    except Exception:
        pass
    async with _step_lock(msg.from_user.id):
        if await _in_state(state, ConnectStates.waiting_code):
            await _submit_code(msg.bot, msg.chat.id, msg.from_user, state, code)


async def _submit_code(bot, chat_id: int, user: types.User, state: FSMContext, code: str) -> None:
    client = _active_clients.get(user.id)
    fsm = await state.get_data()
    if not client:
        await state.clear()
        await bot.send_message(
            chat_id,
            f"{EMO['warn']} Сесію перервано. Почніть заново через «{h(BTN_CONNECT)}».",
            reply_markup=main_menu_kb(user),
        )
        return

    try:
        await client.sign_in(phone=fsm.get("phone"), code=code, phone_code_hash=fsm.get("phone_code_hash"))
    except SessionPasswordNeededError:
        await _update_code_prompt(bot, chat_id, fsm, code, note="✅ <i>Код прийнято.</i>", keypad=False)
        await state.set_state(ConnectStates.waiting_password)
        await bot.send_message(chat_id, _QR_2FA_PROMPT, reply_markup=cancel_kb())
        return
    except PhoneCodeInvalidError:
        await state.update_data(code_input="")
        await _update_code_prompt(
            bot, chat_id, fsm, "",
            note="❌ <b>Код не підійшов.</b> <i>Перевірте останній код від Telegram і наберіть ще раз.</i>",
        )
        return
    except PhoneCodeExpiredError:
        await _update_code_prompt(bot, chat_id, fsm, code, note="⌛ <b>Код прострочений.</b>", keypad=False)
        await bot.send_message(
            chat_id,
            soft_error(
                "Код вже прострочений",
                body=f"Почніть заново через «{h(BTN_CONNECT)}» — ми надішлемо новий код.",
                retry=False,
            ),
            reply_markup=main_menu_kb(user),
        )
        await _disconnect_active(user.id)
        await state.clear()
        return
    except Exception as e:
        await state.update_data(code_input="")
        await _update_code_prompt(
            bot, chat_id, fsm, "",
            note=f"❌ <b>Не вийшло авторизуватися:</b> <code>{h(str(e))}</code>",
        )
        return

    await _update_code_prompt(bot, chat_id, fsm, code, note="✅ <i>Код прийнято.</i>", keypad=False)
    await _finish_success_core(bot, chat_id, user, state)


# ===================== Повторне надсилання коду через SMS =====================
@router.callback_query(F.data == "connect:resend_sms", ConnectStates.waiting_code)
async def resend_sms_code(call: types.CallbackQuery, state: FSMContext) -> None:
    client = _active_clients.get(call.from_user.id)
    if not client:
        await call.answer(
            "Сесія втрачена — натисніть «🔌 Підключити» і почніть заново.",
            show_alert=True,
        )
        return

    fsm = await state.get_data()
    phone = fsm.get("phone")
    if not phone:
        await call.answer("Не знаю вашого номера. Почніть заново.", show_alert=True)
        return

    await call.answer("Просимо Telegram надіслати SMS…")

    try:
        sent = await client.send_code_request(phone, force_sms=True)
    except Exception as exc:
        log.warning("connect: resend SMS failed: %s", exc)
        await call.message.answer(
            soft_error(
                "Не вдалось замовити SMS",
                body=(
                    f"<code>{h(str(exc))}</code>\n\n"
                    "<i>Іноді Telegram блокує повторні запити на короткий час. "
                    "Зачекайте 1–2 хвилини і спробуйте ще раз.</i>"
                ),
                retry=False,
            )
        )
        return

    log.info(
        "connect: resend SMS ok phone=%s type=%s",
        phone,
        type(sent.type).__name__ if sent.type else "?",
    )
    # Старий код більше не діє — стару клавіатуру прибираємо, надсилаємо нову
    await _update_code_prompt(call.bot, call.message.chat.id, fsm, "", note="🔁 <i>Надіслано новий код — див. нижче.</i>", keypad=False)
    await _send_code_keypad(call.bot, call.message.chat.id, state, sent)


# ===================== Крок 3: 2FA =====================
@router.message(ConnectStates.waiting_password)
async def step_password(msg: types.Message, state: FSMContext) -> None:
    password = msg.text or ""
    # Видаляємо повідомлення з паролем одразу для безпеки
    try:
        await msg.delete()
    except Exception:
        pass

    async with _step_lock(msg.from_user.id):
        # Повторно надісланий пароль, поки перевірявся перший, — ігноруємо
        if not await _in_state(state, ConnectStates.waiting_password):
            return

        client = _active_clients.get(msg.from_user.id)
        if not client:
            await state.clear()
            await msg.answer(
                f"{EMO['warn']} Сесію перервано. Почніть заново.",
                reply_markup=main_menu_kb(msg.from_user),
            )
            return

        if not password:
            await msg.answer(
                soft_error(
                    "Пароль порожній",
                    body="Введіть свій cloud password від Telegram (двофакторна автентифікація).",
                )
            )
            return

        checking = await msg.answer("⏳ <i>Перевіряю пароль…</i>")
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
        finally:
            try:
                await checking.delete()
            except Exception:
                pass

        await _finish_success(msg, state)


# ===================== Завершення =====================
async def _finish_success_core(
    bot, chat_id: int, user: types.User, state: FSMContext
) -> None:
    # Ключі, з якими створено сесію, потрібні розсильнику та профілю
    fsm = await state.get_data()
    if fsm.get("api_id") and fsm.get("api_hash"):
        data = load_user(user)
        data.update(
            {
                "user_id": user.id,
                "user_name": user.username,
                "api_id": int(fsm["api_id"]),
                "api_hash": str(fsm["api_hash"]),
            }
        )
        save_user(user, data)

    await _disconnect_active(user.id)
    await state.clear()

    success_text = (
        f"🎉  <b>Готово! Сесію створено.</b>\n"
        f"{DIV}\n"
        f"Тепер бот зможе надсилати повідомлення <b>від вашого імені</b> "
        f"у вибрані чати на тривогу й відбій у Києві.\n\n"
        f"<b>Далі:</b>\n"
        f"  ①  🎯  <b>Обрати чати</b>  →  куди й що надсилати\n"
        f"  ②  ▶️  <b>Увімкнути</b>  →  і бот працює сам\n\n"
        f"{EMO['warn']}  <i>Якщо бот не реагує — перевірте, чи активний "
        f"доступ у «💳 Оплата».</i>"
    )
    await bot.send_message(chat_id, success_text, reply_markup=main_menu_kb(user))
    await bot.send_message(
        chat_id,
        f"{EMO['bolt']}  <b>Хочете налаштувати розсилку зараз?</b>\n"
        f"<i>Це найцікавіша частина — обираємо чати та що надсилати.</i>",
        reply_markup=connect_post_success_kb(),
    )


async def _finish_success(msg: types.Message, state: FSMContext) -> None:
    await _finish_success_core(msg.bot, msg.chat.id, msg.from_user, state)


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
