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
    connect_method_kb,
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


def _resend_keyboard(can_resend_sms: bool) -> Optional[types.InlineKeyboardMarkup]:
    """Inline-клавіатура з кнопкою повторного надсилання."""
    rows: list[list[types.InlineKeyboardButton]] = []
    if can_resend_sms:
        rows.append([types.InlineKeyboardButton(
            text="🔁  Надіслати код через SMS",
            callback_data="connect:resend_sms",
        )])
    rows.append([types.InlineKeyboardButton(
        text="↩️  Скасувати підключення",
        callback_data="connect:cancel",
    )])
    return types.InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


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
    # Якщо є активний Telethon-клієнт (з кроку телефону) — закриваємо
    await _disconnect_active(call.from_user.id)
    await state.clear()
    await call.answer("Скасовано")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.message.answer("❎ Скасовано.", reply_markup=main_menu_kb(call.from_user))


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

    fsm_data = await state.get_data()
    phone = fsm_data.get("phone")
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
                    "Зачекайте 1–2 хвилини і спробуйте ще раз. "
                    "Або скасуйте і почніть з «🔌 Підключити».</i>"
                ),
                retry=False,
            )
        )
        return

    # Оновлюємо phone_code_hash — старий більше не дійсний
    await state.update_data(phone_code_hash=sent.phone_code_hash)

    where = _sent_code_where(sent)
    log.info(
        "connect: resend SMS ok phone=%s type=%s",
        phone,
        type(sent.type).__name__ if sent.type else "?",
    )

    # Чи лишилась можливість попросити ще раз?
    can_resend_again = sent.next_type is not None

    await call.message.answer(
        f"📨  <b>Код надіслано повторно.</b>\n\n"
        f"<b>Куди:</b>\n   {where}\n\n"
        f"<i>Введіть отриманий код одним повідомленням. "
        f"Старий код більше не діє.</i>",
        reply_markup=_resend_keyboard(can_resend_again),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "connect:start")
async def begin_via_button(call: types.CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await _begin_credentials(call.message, state, call.from_user)


async def _begin_credentials(msg: types.Message, state: FSMContext, user: types.User) -> None:
    await state.set_state(ConnectStates.waiting_credentials)
    text = (
        f"{big_step_header(1, 4, 'Ключі API', emoji=EMO['key'])}\n\n"
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

    # Окреме коротке повідомлення з reply-клавіатурою «↩️ Скасувати»,
    # щоб у користувача завжди була під рукою кнопка виходу з майстра.
    await msg.answer(
        "<i>👆 Натисніть кнопку щоб відкрити сайт, або вставте ключі сюди.</i>",
        reply_markup=cancel_kb(),
    )


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
    # Стан скидаємо — далі вибір способу йде через inline-кнопки
    await state.set_state(None)

    await msg.answer(
        f"{EMO['ok']}  <b>Чудово!</b>  Ключі прийнято: "
        f"<code>api_id={api_id}</code>\n\n"
        f"{big_step_header(2, 3, 'Спосіб входу', emoji=EMO['key'])}\n\n"
        f"<b>🔳 QR-код</b> — найнадійніше. Відкриваєте свій Telegram, "
        f"тапаєте по кнопці (або скануєте QR) — і все.\n"
        f"<i>Працює навіть коли код входу не приходить.</i>\n\n"
        f"<b>🔢 Код / SMS</b> — класичний спосіб: Telegram надішле код у застосунок.\n\n"
        f"{tip('якщо код раніше не приходив — обирайте QR.')}",
        reply_markup=connect_method_kb(),
    )


# ===================== Вибір способу входу =====================
_QR_CAPTION = (
    "🔳  <b>Вхід через QR-код</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "<b>Спосіб 1 — з цього ж телефону:</b>\n"
    "натисніть кнопку <b>«✅ Підтвердити вхід»</b> нижче — відкриється "
    "ваш Telegram, лишиться підтвердити вхід.\n\n"
    "<b>Спосіб 2 — з іншого пристрою:</b>\n"
    "Telegram → <b>Налаштування → Пристрої → Підключити пристрій</b> → "
    "наведіть камеру на цей QR.\n\n"
    "<i>Код діє близько хвилини й оновлюється сам, поки ви не підтвердите.</i>"
)

_QR_2FA_PROMPT = (
    "🔐  <b>Потрібен пароль 2FA</b>\n"
    "На вашому акаунті ввімкнено двофакторну автентифікацію.\n"
    "Введіть свій <b>cloud password</b> від Telegram <u>точно як є</u>, "
    "одним повідомленням.\n\n"
    "<i>Повідомлення з паролем буде видалено одразу після отримання.</i>"
)


@router.callback_query(F.data == "connect:method_code")
async def method_code(call: types.CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await state.set_state(ConnectStates.waiting_phone)
    await call.message.answer(
        f"{big_step_header(2, 3, 'Номер телефону', emoji=EMO['phone'])}\n\n"
        f"Надішліть номер вашого Telegram у міжнародному форматі.\n\n"
        f"{example_block('+380501234567', '+380 50 123 45 67')}\n\n"
        f"{tip('пробіли і дужки приберу автоматично — головне щоб був код країни.')}",
        reply_markup=cancel_kb(),
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

    where = _sent_code_where(sent)
    # SMS можна попросити повторно тільки якщо Telegram дозволяє наступний тип
    can_resend_sms = sent.next_type is not None

    log.info(
        "connect: code requested phone=%s type=%s next=%s timeout=%s",
        phone,
        type(sent.type).__name__ if sent.type else "?",
        type(sent.next_type).__name__ if sent.next_type else "—",
        getattr(sent, "timeout", "?"),
    )

    await msg.answer(
        f"{EMO['ok']}  <b>Номер прийнято.</b>\n\n"
        f"{big_step_header(3, 3, 'Код підтвердження', emoji=EMO['code'])}\n\n"
        f"📨  <b>Куди надіслано код:</b>\n   {where}\n\n"
        f"Введіть код одним повідомленням.\n\n"
        f"{example_block('1 2 3 4 5', '12345', '1-2-3-4-5')}\n\n"
        f"{tip('пробіли, дужки і тире — не проблема, лишаються тільки цифри.')}\n\n"
        f"<i>Якщо коду немає протягом хвилини — натисніть «🔁 Надіслати код через SMS» нижче.</i>",
        reply_markup=_resend_keyboard(can_resend_sms),
        disable_web_page_preview=True,
    )
    # Окремо — reply-клавіатура з «Скасувати» (щоб була завжди під рукою)
    await msg.answer(
        "<i>👆 Очікую код від Telegram.</i>",
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
async def _finish_success_core(
    bot, chat_id: int, user: types.User, state: FSMContext
) -> None:
    await _disconnect_active(user.id)
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
