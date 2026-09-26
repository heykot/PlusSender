"""Клавіатури (Reply + Inline)."""
from __future__ import annotations

from aiogram import types

from ..config import (
    BTN_BROADCAST,
    BTN_CHOOSE_CHATS,
    BTN_CANCEL,
    BTN_CONNECT,
    BTN_CONNECT_QR,
    BTN_HELP,
    BTN_MORE,
    BTN_OWN_KEYS,
    BTN_PAYMENT,
    BTN_PROFILE,
    BTN_REFERRAL,
    BTN_SUPPORT,
    BTN_TURN_OFF,
    BTN_TURN_ON,
)
from ..storage import get_targets, has_session, load_user
from ..utils import truncate


# ===================== REPLY-клавіатури =====================
def main_menu_kb(user: types.User) -> types.ReplyKeyboardMarkup:
    """Головне меню залежить від етапу: спершу одна головна дія
    (підключити → обрати чати), далі — робоче меню з перемикачем."""
    data = load_user(user)
    B = types.KeyboardButton
    bottom = [B(text=BTN_PROFILE), B(text=BTN_PAYMENT), B(text=BTN_MORE)]

    if not has_session(user):
        rows = [[B(text=BTN_CONNECT)], [B(text=BTN_PAYMENT), B(text=BTN_MORE)]]
        placeholder = "Почніть з «🔌 Підключити»"
    elif not get_targets(data):
        rows = [[B(text=BTN_CHOOSE_CHATS)], bottom]
        placeholder = "Далі — оберіть чати"
    else:
        toggle = BTN_TURN_OFF if data.get("status") else BTN_TURN_ON
        rows = [[B(text=BTN_BROADCAST)], [B(text=toggle)], bottom]
        placeholder = "Оберіть дію…"

    return types.ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder=placeholder,
    )


def more_menu_kb(connected: bool) -> types.InlineKeyboardMarkup:
    """Рідковживані розділи — під кнопкою «☰ Ще»."""
    I = types.InlineKeyboardButton
    rows = [
        [I(text=BTN_REFERRAL, callback_data="more:referral")],
        [I(text=BTN_SUPPORT, callback_data="more:support"), I(text=BTN_HELP, callback_data="more:help")],
    ]
    if connected:
        rows.append([I(text="🔌 Перепідключити Telegram", callback_data="more:connect")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
    )


# ===================== INLINE — розклад роботи =====================
def schedule_kb(enabled: bool, from_time: str, to_time: str) -> types.InlineKeyboardMarkup:
    rows: list[list[types.InlineKeyboardButton]] = []

    if enabled:
        rows.append([types.InlineKeyboardButton(
            text=f"🟢 Активний: {from_time} — {to_time}",
            callback_data="sched:noop",
        )])
        rows.append([types.InlineKeyboardButton(
            text="✏️ Змінити час",
            callback_data="sched:edit",
        )])
        rows.append([types.InlineKeyboardButton(
            text="🔴 Вимкнути час роботи",
            callback_data="sched:disable",
        )])
    else:
        rows.append([types.InlineKeyboardButton(
            text="🔴 Час роботи вимкнено (надсилати завжди)",
            callback_data="sched:noop",
        )])
        rows.append([types.InlineKeyboardButton(
            text="✏️ Встановити час роботи",
            callback_data="sched:edit",
        )])

    rows.append([types.InlineKeyboardButton(text="‹ Назад", callback_data="st:home")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


# ===================== INLINE — вибір чату-джерела (Telethon-діалоги) =====================
def source_chat_select_kb(
    items: list[dict],
    mapping: dict[str, int],           # key → pid (заповнюється зовні)
    back_cb: str,
) -> types.InlineKeyboardMarkup:
    """Список діалогів для вибору чату-джерела."""
    rows: list[list[types.InlineKeyboardButton]] = [[types.InlineKeyboardButton(
        text="➕ Створити новий чат для кружків",
        callback_data="bset:tc_src_new",
    )]]
    for key, pid in mapping.items():
        item = next((it for it in items if int(it["pid"]) == pid), None)
        if not item:
            continue
        title = truncate(item.get("title") or "—", 30)
        u = f"  @{item['username']}" if item.get("username") else ""
        rows.append([types.InlineKeyboardButton(
            text=f"{title}{u}",
            callback_data=f"bset:tc_src:{key}",
        )])
    rows.append([
        types.InlineKeyboardButton(text="🔍 Пошук", callback_data="bset:tc_src_search"),
        types.InlineKeyboardButton(text="‹ Назад", callback_data=back_cb),
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


# ===================== INLINE — wizard підключення =====================
def connect_phone_kb(own_keys: bool) -> types.ReplyKeyboardMarkup:
    """Reply-клавіатура кроку телефону. Усі варіанти — тут, щоб крок
    вміщався в одне повідомлення (request_contact можливий лише в reply-клавіатурі,
    а reply та inline не можна прикріпити до одного повідомлення)."""
    rows = [
        [types.KeyboardButton(text="📱 Поділитися номером", request_contact=True)],
        [types.KeyboardButton(text=BTN_CONNECT_QR)],
    ]
    if own_keys:
        rows.append([types.KeyboardButton(text=BTN_OWN_KEYS)])
    rows.append([types.KeyboardButton(text=BTN_CANCEL)])
    return types.ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="або введіть номер: +380…",
    )


def code_keypad_kb(can_resend_sms: bool) -> types.InlineKeyboardMarkup:
    """Цифрова клавіатура для коду входу.

    Код набирається кнопками й не потрапляє в чат повідомленням: якщо
    надіслати код текстом, Telegram вважає його «пересланим» і блокує вхід.
    """
    def d(n: str) -> types.InlineKeyboardButton:
        return types.InlineKeyboardButton(text=n, callback_data=f"code:d:{n}")

    rows = [
        [d("1"), d("2"), d("3")],
        [d("4"), d("5"), d("6")],
        [d("7"), d("8"), d("9")],
        [
            types.InlineKeyboardButton(text="⌫", callback_data="code:back"),
            d("0"),
            types.InlineKeyboardButton(text="✅", callback_data="code:ok"),
        ],
    ]
    if can_resend_sms:
        rows.append([types.InlineKeyboardButton(
            text="🔁 Код не прийшов — надіслати SMS",
            callback_data="connect:resend_sms",
        )])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def connect_qr_kb(login_url: str) -> types.InlineKeyboardMarkup:
    """Кнопка-посилання для входу по тапу (на тому ж телефоні) + скасування."""
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(
                text="✅ Підтвердити вхід у Telegram",
                url=login_url,
            )],
            [types.InlineKeyboardButton(
                text="↩️ Скасувати",
                callback_data="connect:cancel",
            )],
        ]
    )


def connect_post_success_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="🎯 Налаштувати розсилку",
                    callback_data="connect:open_broadcast",
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="👤 Профіль", callback_data="connect:open_profile"
                )
            ],
        ]
    )


def connect_existing_session_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="✅ Лишити поточну",
                    callback_data="connect:keep",
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="🔄 Створити нову (видалити стару)",
                    callback_data="connect:replace",
                )
            ],
        ]
    )
