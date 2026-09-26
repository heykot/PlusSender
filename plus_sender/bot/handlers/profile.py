"""Профіль: коротко — чи працює бот і чому ні.

Деталі (що саме йде в кожен чат) — на екрані «🎛 Налаштування»,
тут лише зведення, щоб профіль відкривався миттєво і без Telegram-запитів.
"""
from __future__ import annotations

from aiogram import F, Router, types

from ...config import BTN_BROADCAST, BTN_CONNECT, BTN_PAYMENT, BTN_PROFILE, BTN_TURN_ON, HR
from ...storage import (
    access_days_left,
    get_access_until,
    get_schedule,
    get_targets,
    has_session,
    load_user,
)
from ...utils import h, next_hint, truncate
from ..keyboards import main_menu_kb
from .broadcast import _is_unset, _target_title

router = Router(name="profile")


def _access_line(data: dict) -> str:
    until = get_access_until(data)
    left = access_days_left(data)
    if left is not None:
        tail = "сьогодні останній день" if left == 0 else f"ще {left} дн."
        return f"✅ до {until:%d.%m.%Y} ({tail})"
    if until:
        return f"❌ закінчився {until:%d.%m.%Y}"
    return "❌ немає"


def _chats_line(data: dict) -> str:
    targets = get_targets(data)
    if not targets:
        return "не обрано"
    names = []
    for pid in targets:
        unset = any(_is_unset(data, pid, m) for m in ("alert", "clear"))
        names.append(h(truncate(_target_title(data, pid), 24)) + (" ⚠️" if unset else ""))
    return f"({len(targets)}) " + ", ".join(names)


@router.message(F.text == BTN_PROFILE)
async def show_profile(msg: types.Message) -> None:
    user = msg.from_user
    data = load_user(user)
    connected = has_session(user)
    targets = get_targets(data)
    active = bool(data.get("status"))
    paid = access_days_left(data) is not None
    sched = get_schedule(data)
    hours = f"з {sched['from_time']} до {sched['to_time']}" if sched["enabled"] else "цілодобово"

    text = (
        f"👤  <b>Профіль</b>\n{HR}\n\n"
        f"{'🟢 Бот увімкнений' if active else '⏸ Бот вимкнений'}\n"
        f"📅 Доступ: {_access_line(data)}\n"
        f"📱 Telegram: {'✅ підключено' if connected else '❌ не підключено'}\n"
        f"💬 Чати: {_chats_line(data)}\n"
        f"⏰ Працює: {hours}"
    )

    if not connected:
        hint = next_hint(f"підключіть Telegram через «{BTN_CONNECT}».")
    elif not targets:
        hint = next_hint("оберіть чати через «🎯 Обрати чати».")
    elif any(_is_unset(data, pid, m) for pid in targets for m in ("alert", "clear")):
        hint = next_hint(f"у чатах з ⚠️ не обрано, що надсилати — «{BTN_BROADCAST}».")
    elif not paid:
        hint = next_hint(f"продовжте доступ у «{BTN_PAYMENT}».")
    elif not active:
        hint = next_hint(f"натисніть «{BTN_TURN_ON}».")
    else:
        hint = f"<i>Все працює. Що саме надсилається — у «{BTN_BROADCAST}».</i>"

    await msg.answer(f"{text}\n\n{hint}", reply_markup=main_menu_kb(user))
