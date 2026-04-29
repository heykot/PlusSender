"""Клавіатури (Reply + Inline)."""
from __future__ import annotations

from typing import Optional

from aiogram import types

from ..config import (
    BTN_BROADCAST,
    BTN_CANCEL,
    BTN_CONNECT,
    BTN_DISABLE_TEXT,
    BTN_HELP,
    BTN_PAYMENT,
    BTN_PROFILE,
    BTN_START,
    BTN_STATUS_PREFIX,
    BTN_STOP,
)
from ..storage import get_status
from ..utils import status_label, truncate


# ===================== REPLY-клавіатури =====================
def main_menu_kb(user: types.User) -> types.ReplyKeyboardMarkup:
    active = get_status(user)
    status_btn = f"{BTN_STATUS_PREFIX} {status_label(active)}"
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text=BTN_BROADCAST)],
            [types.KeyboardButton(text=BTN_START), types.KeyboardButton(text=BTN_STOP)],
            [types.KeyboardButton(text=BTN_CONNECT), types.KeyboardButton(text=BTN_PROFILE)],
            [types.KeyboardButton(text=BTN_PAYMENT), types.KeyboardButton(text=BTN_HELP)],
            [types.KeyboardButton(text=status_btn)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Оберіть дію або введіть команду…",
    )


def cancel_kb() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
    )


def text_input_kb() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text=BTN_DISABLE_TEXT)],
            [types.KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True,
    )


# ===================== INLINE — вибір чатів для розсилки =====================
def broadcast_settings_kb(
    items: list[dict],
    selected: list[int],
) -> types.InlineKeyboardMarkup:
    selected_set = set(selected)
    rows: list[list[types.InlineKeyboardButton]] = []
    row: list[types.InlineKeyboardButton] = []

    for it in items[:24]:
        pid = int(it["pid"])
        mark = "✅" if pid in selected_set else "⬜️"
        u = f" @{it.get('username')}" if it.get("username") else ""
        label = f"{mark} {truncate((it.get('title') or '—') + u, 24)}"
        row.append(types.InlineKeyboardButton(text=label, callback_data=f"bset:toggle:{pid}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([types.InlineKeyboardButton(text="🔍 Пошук чату", callback_data="bset:search")])
    rows.append([types.InlineKeyboardButton(text="⚙️ Налаштування кожного чату", callback_data="bset:chatsettings")])
    rows.append([types.InlineKeyboardButton(text="⏰ Розклад роботи", callback_data="bset:schedule")])
    rows.append([
        types.InlineKeyboardButton(text="📊 Поточний стан", callback_data="bset:show"),
        types.InlineKeyboardButton(text="🗑 Скинути чати", callback_data="bset:clear"),
    ])
    rows.append([types.InlineKeyboardButton(text="✅ Зберегти та закрити", callback_data="bset:done")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


# ===================== INLINE — список чатів для per-target налаштувань =====================
def target_list_kb(
    targets: list[int],
    titles: dict[int, str],
    configs: dict[int, dict],          # {pid: {"alert_type": ..., "clear_type": ...}}
) -> tuple[types.InlineKeyboardMarkup, dict[str, int]]:
    """Повертає (клавіатура, mapping key→pid)."""
    mapping: dict[str, int] = {}
    rows: list[list[types.InlineKeyboardButton]] = []
    row: list[types.InlineKeyboardButton] = []

    for idx, pid in enumerate(targets[:24], start=1):
        title = truncate(titles.get(pid, str(pid)), 22)
        cfg = configs.get(pid) or {}
        a_type = cfg.get("alert_type")
        c_type = cfg.get("clear_type")
        if a_type and c_type:
            mark = "⚙️"   # обидва налаштовані
        elif a_type or c_type:
            mark = "📝"   # один налаштований
        else:
            mark = "↩️"   # дефолт
        key = str(idx)
        mapping[key] = pid
        row.append(types.InlineKeyboardButton(
            text=f"{mark} {title}",
            callback_data=f"bset:textchat:{key}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([types.InlineKeyboardButton(text="↩️ Назад до налаштувань", callback_data="bset:textdone")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows), mapping


# ===================== INLINE — налаштування конкретного чату =====================
def _type_icon(type_str: Optional[str]) -> str:
    return {"text": "✍️", "forward": "🎥", "none": "🚫"}.get(type_str or "", "↩️")


def target_chat_kb(
    alert_type: Optional[str],      # "text" | "forward" | "none" | None
    alert_hint: Optional[str],      # короткий опис поточного значення
    alert_delay: int,
    clear_type: Optional[str],
    clear_hint: Optional[str],
    clear_delay: int,
) -> types.InlineKeyboardMarkup:
    rows: list[list[types.InlineKeyboardButton]] = []

    # Кнопка тривоги
    a_icon = _type_icon(alert_type)
    a_label = f"🚨 Тривога  {a_icon}"
    if alert_hint:
        a_label += f"  {truncate(alert_hint, 20)}"
    if alert_type not in (None, "none"):
        a_label += f"  ⏱{alert_delay}с"
    rows.append([types.InlineKeyboardButton(text=a_label, callback_data="bset:tc_a")])

    # Кнопка відбою
    c_icon = _type_icon(clear_type)
    c_label = f"✅ Відбій  {c_icon}"
    if clear_hint:
        c_label += f"  {truncate(clear_hint, 20)}"
    if clear_type not in (None, "none"):
        c_label += f"  ⏱{clear_delay}с"
    rows.append([types.InlineKeyboardButton(text=c_label, callback_data="bset:tc_c")])

    rows.append([
        types.InlineKeyboardButton(text="🗑 Скинути до дефолту", callback_data="bset:tc_reset"),
        types.InlineKeyboardButton(text="↩️ До списку", callback_data="bset:tc_back"),
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


# ===================== INLINE — вибір типу повідомлення для режиму =====================
def target_mode_type_kb(
    mode: str,
    current_type: Optional[str],
    source_title: Optional[str] = None,
) -> types.InlineKeyboardMarkup:
    """Клавіатура вибору: текст / пересилати / не надсилати."""
    rows: list[list[types.InlineKeyboardButton]] = []

    def _check(t: str) -> str:
        return "✅ " if current_type == t else ""

    rows.append([types.InlineKeyboardButton(
        text=f"{_check('text')}✍️ Текст",
        callback_data=f"bset:tc_type:{mode}:text",
    )])

    fwd_label = f"{_check('forward')}🎥 Кружок з чату"
    if source_title and current_type == "forward":
        fwd_label += f"  ({truncate(source_title, 18)})"
    rows.append([types.InlineKeyboardButton(
        text=fwd_label,
        callback_data=f"bset:tc_type:{mode}:forward",
    )])

    rows.append([types.InlineKeyboardButton(
        text=f"{_check('none')}🚫 Не надсилати",
        callback_data=f"bset:tc_type:{mode}:none",
    )])

    rows.append([types.InlineKeyboardButton(
        text="↩️ Назад",
        callback_data="bset:tc_modeback",
    )])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


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
            text="🔴 Вимкнути розклад",
            callback_data="sched:disable",
        )])
    else:
        rows.append([types.InlineKeyboardButton(
            text="🔴 Розклад вимкнено (надсилати завжди)",
            callback_data="sched:noop",
        )])
        rows.append([types.InlineKeyboardButton(
            text="✏️ Встановити час роботи",
            callback_data="sched:edit",
        )])

    rows.append([types.InlineKeyboardButton(text="↩️ Назад", callback_data="bset:sched_back")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


# ===================== INLINE — вибір чату-джерела (Telethon-діалоги) =====================
def source_chat_select_kb(
    items: list[dict],
    mapping: dict[str, int],           # key → pid (заповнюється зовні)
) -> types.InlineKeyboardMarkup:
    """Список діалогів для вибору чату-джерела."""
    rows: list[list[types.InlineKeyboardButton]] = []
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
    rows.append([types.InlineKeyboardButton(text="🔍 Пошук за назвою / ID", callback_data="bset:tc_src_search")])
    rows.append([types.InlineKeyboardButton(text="↩️ Назад", callback_data="bset:tc_modeback")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


# ===================== INLINE — wizard підключення =====================
def connect_intro_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="🌐 Відкрити my.telegram.org",
                    url="https://my.telegram.org/auth",
                )
            ],
            [types.InlineKeyboardButton(text="🚀 Почати", callback_data="connect:start")],
            [types.InlineKeyboardButton(text="↩️ Скасувати", callback_data="connect:cancel")],
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


# ===================== INLINE — admin =====================
def admin_root_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text="👥 Користувачі", callback_data="admin:users"),
                types.InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
            ],
            [
                types.InlineKeyboardButton(text="📨 Розсилка", callback_data="admin:broadcast"),
                types.InlineKeyboardButton(text="👮 Адміни", callback_data="admin:admins"),
            ],
            [
                types.InlineKeyboardButton(text="🧪 Тест тривоги", callback_data="admin:test_alert"),
                types.InlineKeyboardButton(text="🧪 Тест відбою", callback_data="admin:test_clear"),
            ],
        ]
    )


def admin_user_list_kb(
    users: dict[int, dict],
    page: int = 0,
    page_size: int = 10,
) -> types.InlineKeyboardMarkup:
    """Список юзерів з пагінацією."""
    items = list(users.items())
    total = len(items)
    start = page * page_size
    chunk = items[start: start + page_size]
    rows: list[list[types.InlineKeyboardButton]] = []

    for uid, data in chunk:
        uname = data.get("user_name") or f"id{uid}"
        active = "🟢" if data.get("status") else "🔴"
        access = str(data.get("access_until") or "—")[:10]
        rows.append([types.InlineKeyboardButton(
            text=f"{active} @{uname}  [{access}]",
            callback_data=f"admu:view:{uid}",
        )])

    nav: list[types.InlineKeyboardButton] = []
    if page > 0:
        nav.append(types.InlineKeyboardButton(text="◀️", callback_data=f"admu:page:{page-1}"))
    if start + page_size < total:
        nav.append(types.InlineKeyboardButton(text="▶️", callback_data=f"admu:page:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([types.InlineKeyboardButton(text="↩️ Назад", callback_data="admin:back")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_detail_kb(uid: int, active: bool) -> types.InlineKeyboardMarkup:
    """Дії над конкретним користувачем."""
    toggle_text = "❌ Вимкнути" if active else "✅ Увімкнути"
    toggle_cb = f"admu:disable:{uid}" if active else f"admu:enable:{uid}"
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text=toggle_text, callback_data=toggle_cb),
            types.InlineKeyboardButton(text="📨 Написати", callback_data=f"admu:msg:{uid}"),
        ],
        [
            types.InlineKeyboardButton(text="➕30 дн.", callback_data=f"admu:add30:{uid}"),
            types.InlineKeyboardButton(text="➕90 дн.", callback_data=f"admu:add90:{uid}"),
            types.InlineKeyboardButton(text="➕365 дн.", callback_data=f"admu:add365:{uid}"),
        ],
        [
            types.InlineKeyboardButton(text="📅 Встановити дату", callback_data=f"admu:setdate:{uid}"),
            types.InlineKeyboardButton(text="🚫 Забрати доступ", callback_data=f"admu:revoke:{uid}"),
        ],
        [
            types.InlineKeyboardButton(text="🗑 Видалити профіль", callback_data=f"admu:delete:{uid}"),
        ],
        [types.InlineKeyboardButton(text="↩️ До списку", callback_data="admin:users")],
    ])


def admin_admins_kb(admins: dict[int, str]) -> types.InlineKeyboardMarkup:
    """Список адмінів з можливістю видалення."""
    rows: list[list[types.InlineKeyboardButton]] = []
    for uid, uname in admins.items():
        rows.append([types.InlineKeyboardButton(
            text=f"👮 @{uname or '—'}  ({uid})  🗑",
            callback_data=f"adma:del:{uid}",
        )])
    rows.append([types.InlineKeyboardButton(text="➕ Додати адміна", callback_data="adma:add")])
    rows.append([types.InlineKeyboardButton(text="↩️ Назад", callback_data="admin:back")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)
