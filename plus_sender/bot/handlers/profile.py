"""Профіль: коротко — чи працює бот і чому ні; кнопкою — повна інформація.

Коротка версія не звертається до Telegram і відкривається миттєво.
Повна (prof:full) — деталі по кожному чату, кружки в джерелах,
результат останньої розсилки; «‹ Коротко» (prof:short) згортає назад.
"""
from __future__ import annotations

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest

from ... import notifier, sender
from ...config import BTN_BROADCAST, BTN_CONNECT, BTN_PAYMENT, BTN_PROFILE, BTN_TURN_ON, HR, REFERRAL_BONUS_DAYS
from ...storage import (
    access_days_left,
    count_referrals_for,
    get_access_until,
    get_schedule,
    get_target_forward_mode,
    get_target_forward_source,
    get_target_type,
    get_targets,
    has_session,
    load_user,
)
from ...utils import h, next_hint, truncate
from .broadcast import _count_circles, _event_line, _is_unset, _target_title

FULL_KB = types.InlineKeyboardMarkup(inline_keyboard=[[
    types.InlineKeyboardButton(text="📋 Повна інформація", callback_data="prof:full")]])
SHORT_KB = types.InlineKeyboardMarkup(inline_keyboard=[[
    types.InlineKeyboardButton(text="‹ Коротко", callback_data="prof:short")]])

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


def _short_text(user: types.User, data: dict) -> str:
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
    return f"{text}\n\n{hint}"


async def _full_text(user: types.User, data: dict) -> str:
    sched = get_schedule(data)
    hours = f"з {sched['from_time']} до {sched['to_time']}" if sched["enabled"] else "цілодобово"
    lines = [
        f"👤  <b>Профіль — повна інформація</b>\n{HR}\n",
        f"🆔 Ваш ID: <code>{user.id}</code> <i>(для оплати й підтримки)</i>",
        f"{'🟢 Бот увімкнений' if data.get('status') else '⏸ Бот вимкнений'}",
        f"📅 Доступ: {_access_line(data)}",
        f"📱 Telegram: {'✅ підключено' if has_session(user) else '❌ не підключено'}",
        f"⏰ Працює: {hours}",
    ]
    invited, paid = count_referrals_for(user.id)
    if invited:
        lines.append(f"🎁 Запрошено друзів: <b>{invited}</b>, купили: <b>{paid}</b> (+{paid * REFERRAL_BONUS_DAYS} дн.)")

    targets = get_targets(data)
    lines.append(f"\n💬 <b>Чати ({len(targets)})</b>" if targets else "\n💬 Чатів не обрано")
    circles: dict[int, object] = {}
    for pid in targets:
        lines.append(f"\n<b>{h(_target_title(data, pid))}</b>")
        for mode in ("alert", "clear"):
            lines.append(f"   {_event_line(data, pid, mode)}")
            src = get_target_forward_source(data, pid, mode)
            if get_target_type(data, pid, mode) == "forward" and src:
                cid = int(src["chat_id"])
                if cid not in circles:
                    circles[cid] = await _count_circles(user, cid)
                n = circles[cid]
                how = "по колу" if get_target_forward_mode(data, pid, mode) == "roundrobin" else "відправив → видалив"
                count = "недоступно" if n is None else ("⚠️ немає кружків" if n == 0 else f"кружків: {n}")
                lines.append(f"      <i>{how} · {count}</i>")

    run = sender.LAST_RUN
    result = run.get("results", {}).get(str(user.id)) if run else None
    if result:
        ok, _ = result
        problems = sender.LAST_PROBLEMS.get(str(user.id), [])
        status = ("⚠️ надіслано частково" if problems else "✅ надіслано") if ok else "❌ не надіслано"
        event = "тривога" if run["mode"] == "alert" else "відбій"
        lines.append(f"\n📨 <b>Остання розсилка</b> ({event}, {run['at']:%d.%m %H:%M}): {status}")
        for pid, code in sender.LAST_PROBLEMS.get(str(user.id), []):
            if pid is None:
                if code in notifier.SESSION_CODES:
                    lines.append("   • бот втратив доступ до вашого Telegram — перепідключіться")
            elif (t := notifier._problem_text(data, pid, code, run["mode"])):
                lines.append(f"   • {t}")
    return "\n".join(lines)


@router.message(F.text == BTN_PROFILE)
async def show_profile(msg: types.Message) -> None:
    await msg.answer(_short_text(msg.from_user, load_user(msg.from_user)), reply_markup=FULL_KB)


@router.callback_query(F.data.in_({"prof:full", "prof:short"}))
async def cb_profile_view(call: types.CallbackQuery) -> None:
    data = load_user(call.from_user)
    full = call.data == "prof:full"
    await call.answer("Збираю інформацію…" if full else None)
    text = await _full_text(call.from_user, data) if full else _short_text(call.from_user, data)
    try:
        await call.message.edit_text(text, reply_markup=SHORT_KB if full else FULL_KB)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            await call.message.answer(text, reply_markup=SHORT_KB if full else FULL_KB)
